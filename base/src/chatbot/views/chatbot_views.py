from typing import cast

from django.db.models import Case, IntegerField, Q, Value, When
from django.http import Http404, JsonResponse
from django.utils.timezone import now
from drf_yasg import openapi
from drf_yasg.inspectors import SwaggerAutoSchema
from drf_yasg.utils import swagger_auto_schema
from injector import Injector
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from chatbot.api_parameters import request_id_parameter
from chatbot.authentication_mixin import AuthenticationMixin
from chatbot.chunk_retriever_interface import ChunkRetrieverInterface
from chatbot.conversation_factory import ConversationFactory, ConversationType
from chatbot.enums import DocumentType
from chatbot.langsmith_client import (
    LangsmithAuthError,
    LangsmithClient,
    LangsmithError,
    LangsmithNotFoundError,
    LangsmithValidationError,
)
from chatbot.llm_client_interface import CompletionTimeoutError
from chatbot.models import Conversation as Convo
from chatbot.models.info_message import InfoMessage
from chatbot.modules import ChatbotModule
from chatbot.openapi_constants import AUTHORISATION_ERROR_RESPONSES
from chatbot.orchestrator import Orchestrator
from chatbot.project_repository import ProjectNotFoundError
from chatbot.project_service import ProjectService
from chatbot.serializers import (
    ConversationDeletionResponseSerializer,
    ConversationListQuerySerializer,
    ConversationMultipleDeletionRequestSerializer,
    ConversationMultipleDeletionResponseSerializer,
    ConversationPatchRequestSerializer,
    ConversationResponseSerializer,
    ConversationSubmissionResponseSerializer,
    DocumentDeletionRequestSerializer,
    DocumentResponseSerializer,
    DocumentSubmissionRequestSerializer,
    FeedbackBatchRequestSerializer,
    FeedbackBatchResponseSerializer,
    InfoMessageSerializer,
    MessageResponseSerializer,
    MessageSubmissionRequestSerializer,
)
from chatbot.user_info_provider import UserInfoProvider
from chatbot.utils.attachment_format import format_attachment_dtos
from chatbot.utils.file_utils import AttachmentValidationError, extract_attachment
from chatbot.views.project_views import (
    PROJECT_FORBIDDEN_RESPONSE,
    PROJECT_NOT_FOUND_RESPONSE,
)
from chatbot.web_conversation_repository import (
    ConversationNotFoundError,
    MessageNotFoundError,
)
from chatbot.web_conversation_service import (
    ForbiddenMessageAccessError,
    WebConversationService,
)


class MultipartOnlyAutoSchema(SwaggerAutoSchema):
    def get_consumes(self):
        return ["multipart/form-data"]


class ConversationList(AuthenticationMixin, APIView):
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    @swagger_auto_schema(
        manual_parameters=[request_id_parameter],
        query_serializer=ConversationListQuerySerializer,
        responses={
            **AUTHORISATION_ERROR_RESPONSES,
            200: ConversationResponseSerializer(many=True),
            **PROJECT_FORBIDDEN_RESPONSE,
            **PROJECT_NOT_FOUND_RESPONSE,
        },
        operation_description="List my chats outside projects, or one project's chats with project_id.",
    )
    def get(self, request: Request) -> Response:
        query_serializer = ConversationListQuerySerializer(data=request.query_params)
        query_serializer.is_valid(raise_exception=True)

        requested_project_id = query_serializer.validated_data.get("project_id")
        project_id = None
        if requested_project_id:
            try:
                project_id = (
                    ProjectService(request.claims["username"])
                    .get_project(requested_project_id)
                    .id
                )
            except ProjectNotFoundError as e:
                raise NotFound("Project not found.") from e

        injector = Injector(
            ChatbotModule(
                request.logger,
                request.claims["username"],
                request.headers.get("X-Request-ID"),
            )
        )
        factory = injector.get(ConversationFactory)
        service = cast(
            WebConversationService, factory.create_service(ConversationType.WEB)
        )

        conversations_dtos = service.get_conversations(project_id=project_id)
        formatted_conversations = [
            {
                "name": dto.name,
                "created_at": dto.creation_date,
                "updated_at": dto.update_date,
                "id": str(dto.uuid),
                "pinned": dto.pinned,
                "pinned_at": dto.pinned_at,
                "assistant_id": dto.assistant_id,
                "assistant_name": dto.assistant_name,
                "custom_url": dto.custom_url,
                "deployment_id": dto.deployment_id,
                "deployment_name": dto.deployment_name,
                "environment": dto.environment,
                "project_id": dto.project_id,
            }
            for dto in conversations_dtos
        ]
        serializer = ConversationResponseSerializer(formatted_conversations, many=True)
        return Response(serializer.data)

    @swagger_auto_schema(
        auto_schema=MultipartOnlyAutoSchema,
        manual_parameters=[
            request_id_parameter,
            openapi.Parameter(
                "message", openapi.IN_FORM, type=openapi.TYPE_STRING, required=True
            ),
            openapi.Parameter(
                "file",
                openapi.IN_FORM,
                type=openapi.TYPE_FILE,
                required=False,
                description="Fichier à joindre (pdf, image, docx, xlsx, pptx, txt, py...)",
            ),
            openapi.Parameter(
                "all_documents_included",
                openapi.IN_FORM,
                type=openapi.TYPE_BOOLEAN,
                required=False,
            ),
            openapi.Parameter(
                "included_documents",
                openapi.IN_FORM,
                type=openapi.TYPE_ARRAY,
                items=openapi.Items(type=openapi.TYPE_STRING),
                required=False,
            ),
            openapi.Parameter(
                "is_orchestration_delegated",
                openapi.IN_FORM,
                type=openapi.TYPE_BOOLEAN,
                required=False,
            ),
        ],
        responses={
            **AUTHORISATION_ERROR_RESPONSES,
            201: ConversationSubmissionResponseSerializer,
        },
        operation_description="Create a new conversation by giving initial user message",
    )
    def post(self, request):
        request_serializer = MessageSubmissionRequestSerializer(data=request.data)

        if not request_serializer.is_valid():
            return Response(
                request_serializer.errors, status=status.HTTP_400_BAD_REQUEST
            )
        user_message = request_serializer.validated_data["message"]
        all_documents_included = request_serializer.validated_data[
            "all_documents_included"
        ]
        included_documents = request_serializer.validated_data["included_documents"]
        is_orchestration_delegated = request_serializer.validated_data[
            "is_orchestration_delegated"
        ]
        locale = request_serializer.validated_data.get("locale")
        injector = Injector(
            ChatbotModule(
                request.logger,
                request.claims["username"],
                request.headers.get("X-Request-ID"),
                is_orchestration_delegated,
                auth_token=getattr(request, "auth_token", None),
            )
        )
        factory = injector.get(ConversationFactory)
        service = factory.create_service(ConversationType.WEB)

        uid = ""
        try:
            user_info_provider = injector.get(UserInfoProvider)
            user_email = request.claims["username"]
            user_info = user_info_provider.get_user_info_email(user_email)
            if user_info:
                entity_id = getattr(user_info, "uo_id_uo", "")
                uid = user_info.i_uid
        except Exception:
            entity_id = ""
            if request.claims:
                uid = getattr(request.claims, "uid", "")
            else:
                uid = ""

        web_conversation_dto = service.create_conversation()

        uploaded_file = request_serializer.validated_data.get("file")

        attachment = None
        if uploaded_file is not None:
            # TODO: add logger
            try:
                attachment = extract_attachment(uploaded_file)
            except AttachmentValidationError as exc:
                return Response(
                    {"file": [str(exc)]}, status=status.HTTP_400_BAD_REQUEST
                )

        try:
            message_exchange = service.post_message(
                conversation_uuid=web_conversation_dto.uuid,
                user_message=user_message,
                entity_id=entity_id,
                all_documents_included=all_documents_included,
                included_documents=included_documents,
                request_id=request.headers.get("X-Request-ID"),
                uid=uid,
                attachment=attachment,
            )
        except CompletionTimeoutError:
            return JsonResponse(
                {
                    "type": "COMPLETION_TIMEOUT",
                    "title": "Request timed out when trying to make completion.",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        web_conversation_dto = service.generate_title_on_creation(
            web_conversation_dto,
            user_message,
            locale=locale,
        )

        formatted_sources = [
            {
                "title": source.title,
                "content": source.content,
                "source_document": source.source_document,
                "document_url": source.url,
                "document_type": source.document_type.value,
            }
            for source in message_exchange.sources
        ]
        data = {
            "user_message": message_exchange.user_message,
            "message_id": str(message_exchange.uuid),
            "bot_response": message_exchange.bot_response,
            "all_documents_included": request_serializer.validated_data[
                "all_documents_included"
            ],
            "included_documents": request_serializer.validated_data[
                "included_documents"
            ],
            "conversation_name": web_conversation_dto.name,
            "sources": formatted_sources,
            "conversation_creation_date": web_conversation_dto.creation_date,
            "conversation_id": str(web_conversation_dto.uuid),
            "required_orchestrator": (
                Orchestrator.LGP.value
                if is_orchestration_delegated
                else Orchestrator.LISAB.value
            ),
            "actual_orchestrator": (
                Orchestrator.LGP.value
                if is_orchestration_delegated
                else Orchestrator.LISAB.value
            ),
            "inference_caller": message_exchange.inference_caller.value,
            "attachments": format_attachment_dtos(message_exchange.attachments),
        }
        response_serializer = ConversationSubmissionResponseSerializer(data)

        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

    @swagger_auto_schema(
        manual_parameters=[request_id_parameter],
        request_body=ConversationMultipleDeletionRequestSerializer,
        responses={
            **AUTHORISATION_ERROR_RESPONSES,
            200: ConversationMultipleDeletionResponseSerializer,
        },
        operation_description="Delete multiple conversations.",
    )
    def delete(self, request):
        request_serializer = ConversationMultipleDeletionRequestSerializer(
            data=request.data
        )

        if not request_serializer.is_valid():
            return Response(
                request_serializer.errors, status=status.HTTP_400_BAD_REQUEST
            )

        injector = Injector(
            ChatbotModule(
                request.logger,
                request.claims["username"],
                request.headers.get("X-Request-ID"),
            )
        )
        factory = injector.get(ConversationFactory)
        service = factory.create_service(ConversationType.WEB)

        conversation_ids = request_serializer.validated_data["conversation_ids"]
        deleted = []
        errors = {}

        for conversation_id in conversation_ids:
            try:
                service.delete_conversation(conversation_id)
                deleted.append(conversation_id)
            except ConversationNotFoundError:
                errors[conversation_id] = f"Conversation {conversation_id} not found."
            except Exception as e:
                errors[conversation_id] = str(e)

        response_data = {"deleted": deleted, "errors": errors}
        response_serializer = ConversationMultipleDeletionResponseSerializer(
            response_data
        )
        return Response(response_serializer.data, status=status.HTTP_207_MULTI_STATUS)


class Conversation(AuthenticationMixin, APIView):
    @swagger_auto_schema(
        manual_parameters=[request_id_parameter],
        responses={
            **AUTHORISATION_ERROR_RESPONSES,
            200: ConversationResponseSerializer,
        },
        operation_description="Get details of a specific conversation",
    )
    def get(self, request, conversation_id):
        injector = Injector(
            ChatbotModule(
                request.logger,
                request.claims["username"],
                request.headers.get("X-Request-ID"),
            )
        )
        factory = injector.get(ConversationFactory)
        service = factory.create_service(ConversationType.WEB)

        try:
            conversation = service.get_conversation(conversation_id)
            attachments = service.get_conversation_attachments(conversation_id)
        except ConversationNotFoundError as e:
            raise Http404(f"Conversation {conversation_id} not found.") from e

        data = {
            "name": conversation.name,
            "created_at": conversation.creation_date,
            "updated_at": conversation.update_date,
            "id": conversation_id,
            "attachments": format_attachment_dtos(attachments),
            "project_id": conversation.project_id,
        }

        response_serializer = ConversationResponseSerializer(data)
        return Response(response_serializer.data)

    @swagger_auto_schema(
        manual_parameters=[request_id_parameter],
        responses={
            **AUTHORISATION_ERROR_RESPONSES,
            200: ConversationResponseSerializer,
        },
        request_body=ConversationPatchRequestSerializer,
        operation_description="Rename, pin, or move a conversation into a project (project_id is accepted but not applied yet)",
    )
    def patch(self, request, conversation_id):
        request_serializer = ConversationPatchRequestSerializer(data=request.data)
        if not request_serializer.is_valid():
            return Response(
                request_serializer.errors, status=status.HTTP_400_BAD_REQUEST
            )
        validated_data = request_serializer.validated_data

        injector = Injector(
            ChatbotModule(
                request.logger,
                request.claims["username"],
                request.headers.get("X-Request-ID"),
            )
        )
        factory = injector.get(ConversationFactory)
        service = factory.create_service(ConversationType.WEB)

        try:
            conversation = None
            if "name" in validated_data:
                conversation = service.update_conversation_name(
                    conversation_uuid=conversation_id, new_name=validated_data["name"]
                )
            elif "pinned" in validated_data:
                conversation = service.set_conversation_pinned(
                    conversation_uuid=conversation_id,
                    pinned=validated_data["pinned"],
                )
            elif "project_id" in validated_data:
                # Moving chats into projects isn't applied yet: return the chat unchanged.
                conversation = service.get_conversation(conversation_id)
            else:
                raise ValueError(f"Unsupported patch fields: {sorted (validated_data)}")
        except ConversationNotFoundError as e:
            raise Http404(f"Conversation {conversation_id} not found.") from e

        data = {
            "name": conversation.name,
            "created_at": conversation.creation_date,
            "updated_at": conversation.update_date,
            "id": conversation_id,
            "pinned": conversation.pinned,
            "pinned_at": conversation.pinned_at,
            "project_id": conversation.project_id,
        }

        response_serializer = ConversationResponseSerializer(data)
        return Response(response_serializer.data)

    @swagger_auto_schema(
        manual_parameters=[request_id_parameter],
        responses={
            **AUTHORISATION_ERROR_RESPONSES,
            200: ConversationDeletionResponseSerializer,
        },
        operation_description="Delete a specific conversation.",
    )
    def delete(self, request, conversation_id):
        injector = Injector(
            ChatbotModule(
                request.logger,
                request.claims["username"],
                request.headers.get("X-Request-ID"),
            )
        )
        factory = injector.get(ConversationFactory)
        service = factory.create_service(ConversationType.WEB)
        try:
            conversation = service.delete_conversation(conversation_id)
        except ConversationNotFoundError as e:
            raise Http404(f"Conversation {conversation_id} not found.") from e

        data = {
            "name": conversation.name,
            "created_at": conversation.creation_date,
            "updated_at": conversation.update_date,
            "id": conversation_id,
            "project_id": conversation.project_id,
        }
        response_serializer = ConversationResponseSerializer(data)
        return Response(response_serializer.data)


class MessageList(AuthenticationMixin, APIView):
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    @swagger_auto_schema(
        manual_parameters=[request_id_parameter],
        responses={
            **AUTHORISATION_ERROR_RESPONSES,
            200: MessageResponseSerializer(many=True),
        },
        operation_description="Get all messages of a specific conversation",
    )
    def get(self, request, conversation_id):
        injector = Injector(
            ChatbotModule(
                request.logger,
                request.claims["username"],
                request.headers.get("X-Request-ID"),
            )
        )
        factory = injector.get(ConversationFactory)
        service = factory.create_service(ConversationType.WEB)

        try:
            conversation = Convo.objects.select_related("assistant").get(
                uuid=conversation_id
            )
        except Convo.DoesNotExist as e:
            raise ConversationNotFoundError from e

        assistant = conversation.assistant
        messages = service.get_conversation_messages(conversation_uuid=conversation_id)
        formatted_messages = []

        for message in messages:
            formatted_message = {
                "message_id": message.uuid,
                "user_message": message.user_message,
                "bot_response": message.bot_response,
                "sources": [],
                "all_documents_included": False,
                "included_documents": [],
                "attachments": format_attachment_dtos(message.attachments),
            }

            if assistant is not None:
                formatted_message["assistant_id"] = str(assistant.assistant_id)
                formatted_message["assistant_name"] = assistant.assistant_name
                formatted_message["custom_url"] = assistant.url
                formatted_message["environment"] = assistant.environment
                formatted_message["deployment_id"] = str(assistant.deployment_id)
                formatted_message["deployment_name"] = assistant.deployment_name

            formatted_messages.append(formatted_message)

        response_serializer = MessageResponseSerializer(formatted_messages, many=True)
        return Response(response_serializer.data)

    @swagger_auto_schema(
        auto_schema=MultipartOnlyAutoSchema,
        manual_parameters=[
            request_id_parameter,
            openapi.Parameter(
                "message", openapi.IN_FORM, type=openapi.TYPE_STRING, required=True
            ),
            openapi.Parameter(
                "file",
                openapi.IN_FORM,
                type=openapi.TYPE_FILE,
                required=False,
                description="Fichier à joindre (pdf, image, docx, xlsx, pptx, txt, py...)",
            ),
            openapi.Parameter(
                "all_documents_included",
                openapi.IN_FORM,
                type=openapi.TYPE_BOOLEAN,
                required=False,
            ),
            openapi.Parameter(
                "included_documents",
                openapi.IN_FORM,
                type=openapi.TYPE_ARRAY,
                items=openapi.Items(type=openapi.TYPE_STRING),
                required=False,
            ),
            openapi.Parameter(
                "is_orchestration_delegated",
                openapi.IN_FORM,
                type=openapi.TYPE_BOOLEAN,
                required=False,
            ),
        ],
        responses={
            **AUTHORISATION_ERROR_RESPONSES,
            201: MessageResponseSerializer,
        },
        operation_description="Post a new user message and receive bot response",
    )
    def post(self, request, conversation_id):
        request_serializer = MessageSubmissionRequestSerializer(data=request.data)
        if not request_serializer.is_valid():
            return Response(
                request_serializer.errors, status=status.HTTP_400_BAD_REQUEST
            )
        user_message = request_serializer.validated_data["message"]
        all_documents_included = request_serializer.validated_data[
            "all_documents_included"
        ]
        included_documents = request_serializer.validated_data["included_documents"]
        is_orchestration_delegated = request_serializer.validated_data[
            "is_orchestration_delegated"
        ]
        injector = Injector(
            ChatbotModule(
                request.logger,
                request.claims["username"],
                request.headers.get("X-Request-ID"),
                is_orchestration_delegated,
                auth_token=getattr(request, "auth_token", None),
            )
        )
        factory = injector.get(ConversationFactory)
        service = factory.create_service(ConversationType.WEB)

        uid = ""
        try:
            user_info_provider = injector.get(UserInfoProvider)
            user_email = request.claims["username"]
            user_info = user_info_provider.get_user_info_email(user_email)
            if user_info:
                entity_id = getattr(user_info, "uo_id_uo", "")
                uid = user_info.i_uid
        except Exception:
            entity_id = ""
            if request.claims:
                uid = getattr(request.claims, "uid", "")
            else:
                uid = ""

        uploaded_file = request_serializer.validated_data.get("file")

        attachment = None
        if uploaded_file is not None:
            # TODO: add logger
            try:
                attachment = extract_attachment(uploaded_file)
            except AttachmentValidationError as exc:
                return Response(
                    {"file": [str(exc)]}, status=status.HTTP_400_BAD_REQUEST
                )

        try:
            message_exchange = service.post_message(
                user_message=user_message,
                conversation_uuid=conversation_id,
                all_documents_included=all_documents_included,
                included_documents=included_documents,
                request_id=request.headers.get("X-Request-ID"),
                entity_id=entity_id,
                uid=uid,
                attachment=attachment,
            )
        except CompletionTimeoutError:
            return JsonResponse(
                {
                    "type": "COMPLETION_TIMEOUT",
                    "title": "Request timed out when trying to make completion.",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        formatted_sources = [
            {
                "title": source.title,
                "content": source.content,
                "source_document": source.source_document,
                "document_url": source.url,
                "document_type": source.document_type.value,
            }
            for source in message_exchange.sources
        ]

        data = {
            "message_id": str(message_exchange.uuid),
            "user_message": message_exchange.user_message,
            "bot_response": message_exchange.bot_response,
            "sources": formatted_sources,
            "all_documents_included": request_serializer.validated_data[
                "all_documents_included"
            ],
            "included_documents": request_serializer.validated_data[
                "included_documents"
            ],
            "required_orchestrator": (
                Orchestrator.LGP.value
                if is_orchestration_delegated
                else Orchestrator.LISAB.value
            ),
            "actual_orchestrator": (
                Orchestrator.LGP.value
                if is_orchestration_delegated
                else Orchestrator.LISAB.value
            ),
            "inference_caller": message_exchange.inference_caller.value,
            "attachments": format_attachment_dtos(message_exchange.attachments),
        }

        response_serializer = MessageResponseSerializer(data)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)


class Feedback(AuthenticationMixin, APIView):
    @swagger_auto_schema(
        manual_parameters=[request_id_parameter],
        request_body=FeedbackBatchRequestSerializer,
        responses={
            **AUTHORISATION_ERROR_RESPONSES,
            207: FeedbackBatchResponseSerializer,
            400: openapi.Response(
                description=(
                    "Validation error: invalid or empty feedbacks list, bad "
                    "key, or bad score."
                ),
                examples={
                    "application/json": {
                        "feedbacks": [{"score": ["5 is not a valid choice."]}]
                    }
                },
            ),
            403: openapi.Response(
                description="You do not have access to this message.",
                examples={
                    "application/json": {
                        "detail": "You do not have access to this message."
                    }
                },
            ),
            404: openapi.Response(
                description="Message not found.",
                examples={"application/json": {"detail": "Message not found."}},
            ),
            409: openapi.Response(
                description="Feedback is not available for this message (no run id).",
                examples={
                    "application/json": {
                        "detail": "Feedback is not available for this message."
                    }
                },
            ),
        },
        operation_description=(
            "Send a non-empty list of feedback items for a bot response; one "
            "LangSmith record is created per item. Each item has a `key` "
            "(user_score, hallucination, completeness or trajectory), a binary "
            "`score` (0 or 1), an optional `comment`, and an optional "
            "`correction` object (key_facts / key_elements)."
        ),
    )
    def post(self, request, conversation_id, message_id):
        request_serializer = FeedbackBatchRequestSerializer(data=request.data)
        if not request_serializer.is_valid():
            return Response(
                request_serializer.errors, status=status.HTTP_400_BAD_REQUEST
            )

        feedbacks = request_serializer.validated_data["feedbacks"]

        logger = request.logger.clone({"name_space": "FEEDBACK"})

        injector = Injector(
            ChatbotModule(
                request.logger,
                request.claims["username"],
                request.headers.get("X-Request-ID"),
            )
        )
        langsmith_client = injector.get(LangsmithClient)
        factory = injector.get(ConversationFactory)
        service = factory.create_service(ConversationType.WEB)

        try:
            message = service.get_message(message_id)
        except MessageNotFoundError:
            logger.error(
                "FEEDBACK_MESSAGE_NOT_FOUND",
                message_id=message_id,
                conversation_id=conversation_id,
            )
            return Response(
                {"detail": "Message not found."}, status=status.HTTP_404_NOT_FOUND
            )
        except ForbiddenMessageAccessError:
            logger.error(
                "FEEDBACK_FORBIDDEN_MESSAGE_ACCESS",
                message_id=message_id,
                conversation_id=conversation_id,
            )
            return Response(
                {"detail": "You do not have access to this message."},
                status=status.HTTP_403_FORBIDDEN,
            )
        run_id = message.run_id
        if run_id is None:
            logger.warning(
                "FEEDBACK_NO_RUN_ID",
                message_id=message_id,
                conversation_id=conversation_id,
            )
            return Response(
                {"detail": "Feedback is not available for this message."},
                status=status.HTTP_409_CONFLICT,
            )

        submitted = []
        errors = {}

        for index, item in enumerate(feedbacks):
            key = item["key"]
            score = item["score"]
            comment = item.get("comment")
            correction = item.get("correction")
            try:
                langsmith_client.create_feedback(
                    run_id=run_id,
                    key=key,
                    score=score,
                    comment=comment,
                    correction=correction,
                )
            except LangsmithValidationError as e:
                logger.error(
                    "FEEDBACK_VALIDATION_ERROR",
                    message_id=message_id,
                    run_id=run_id,
                    index=index,
                    key=key,
                    error=str(e),
                )
                errors[str(index)] = str(e)
            except LangsmithNotFoundError as e:
                logger.error(
                    "FEEDBACK_RUN_NOT_FOUND",
                    message_id=message_id,
                    run_id=run_id,
                    index=index,
                    key=key,
                    error=str(e),
                )
                errors[str(index)] = (
                    "The run associated with this message was not found."
                )
            except LangsmithAuthError as e:
                logger.error(
                    "FEEDBACK_AUTH_ERROR",
                    message_id=message_id,
                    run_id=run_id,
                    index=index,
                    key=key,
                    error=str(e),
                )
                errors[str(index)] = "LangSmith authentication failed."
            except LangsmithError as e:
                logger.error(
                    "FEEDBACK_LANGSMITH_ERROR",
                    message_id=message_id,
                    run_id=run_id,
                    index=index,
                    key=key,
                    error=str(e),
                )
                errors[str(index)] = "LangSmith is temporarily unavailable."
            else:
                submitted.append(index)

        response_serializer = FeedbackBatchResponseSerializer(
            {"submitted": submitted, "errors": errors}
        )
        return Response(response_serializer.data, status=status.HTTP_207_MULTI_STATUS)


class DocumentList(AuthenticationMixin, APIView):
    parser_classes = [MultiPartParser, FormParser]

    @swagger_auto_schema(
        manual_parameters=[request_id_parameter],
        request_body=DocumentSubmissionRequestSerializer,
        responses={
            **AUTHORISATION_ERROR_RESPONSES,
            201: DocumentResponseSerializer,
        },
        operation_description="Upload a file that will be incorporated to bot context.",
    )
    def post(self, request, format=None):
        request_serializer = DocumentSubmissionRequestSerializer(data=request.data)
        if not request_serializer.is_valid():
            return Response(
                request_serializer.errors, status=status.HTTP_400_BAD_REQUEST
            )

        data = {
            "name": "Dummy file 1",
            "upload_date": "2024-04-29 07:56:40",
            "document_type": "file",
            "document_id": "60b1e996-1f1c-4b2c-94bc-cae3c95b6955",
        }
        response_serializer = DocumentResponseSerializer(data)

        return Response(response_serializer.data)

    @swagger_auto_schema(
        manual_parameters=[request_id_parameter],
        responses={
            **AUTHORISATION_ERROR_RESPONSES,
            200: DocumentResponseSerializer(many=True),
        },
        operation_description="Get a list of available documents usable for bot context.",
    )
    def get(self, request):
        injector = Injector(
            ChatbotModule(
                request.logger,
                request.claims["username"],
                request.headers.get("X-Request-ID"),
            )
        )
        chunk_retriever = injector.get(ChunkRetrieverInterface)
        document_names = chunk_retriever.get_ingested_documents()

        data = []
        for document in document_names:
            document_type = document["document_type"]

            if isinstance(document_type, DocumentType):
                document_type = document_type.value

            data.append(
                {
                    "name": document["name"],
                    "document_type": document_type,
                }
            )

        serializer = DocumentResponseSerializer(data, many=True)
        return Response(serializer.data)

    @swagger_auto_schema(
        manual_parameters=[request_id_parameter],
        responses={
            **AUTHORISATION_ERROR_RESPONSES,
            200: DocumentDeletionRequestSerializer,
        },
        operation_description="Delete a specific document.",
    )
    def delete(self, request):
        data = {
            "name": "Dummy file 2",
            "upload_date": "2023-03-22 08:21:14",
            "document_type": "file",
            "document_id": "60b1e996-1f1c-4b2c-94bc-cae3c95b6955",
        }

        response_serializer = DocumentResponseSerializer(data)
        return Response(response_serializer.data)


class InfoMessageList(AuthenticationMixin, APIView):
    @swagger_auto_schema(
        manual_parameters=[request_id_parameter],
        responses={
            **AUTHORISATION_ERROR_RESPONSES,
            200: ConversationResponseSerializer,
        },
        operation_description="Get info_message messages",
    )
    def get(self, request):
        messages = (
            InfoMessage.objects.filter(
                Q(is_active=True)
                & (Q(expires_at__gt=now()) | Q(expires_at__isnull=True))
            )
            .annotate(
                severity_rank=Case(
                    When(severity="alert", then=Value(0)),
                    When(severity="warning", then=Value(1)),
                    default=Value(2),
                    output_field=IntegerField(),
                )
            )
            .order_by("severity_rank", "-created_at")
        )

        if messages.exists():
            return Response(InfoMessageSerializer(messages, many=True).data)
        return Response([], status=status.HTTP_200_OK)
