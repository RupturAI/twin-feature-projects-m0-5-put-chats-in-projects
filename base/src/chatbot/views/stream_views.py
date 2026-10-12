import json

from django.http import StreamingHttpResponse
from drf_yasg import openapi
from drf_yasg.inspectors import SwaggerAutoSchema
from drf_yasg.utils import swagger_auto_schema
from injector import Injector
from rest_framework import status
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    parser_classes,
    permission_classes,
)
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from chatbot.api_parameters import request_id_parameter
from chatbot.authentication import ChatbotAuth, IsAuthorizedPole, IsTechnicalUser
from chatbot.conversation_factory import ConversationFactory, ConversationType
from chatbot.lgp_client import LgpRewindException
from chatbot.models import Conversation
from chatbot.modules import ChatbotModule
from chatbot.openapi_constants import AUTHORISATION_ERROR_RESPONSES
from chatbot.orchestrator import Orchestrator
from chatbot.serializers import (
    ConversationSubmissionResponseSerializer,
    ConversationSubmissionStreamedSerializer,
    MessageResponseSerializer,
    MessageSubmissionStreamedSerializer,
    StreamedConversationSubmissionRequestSerializer,
    StreamedMessageSubmissionRequestSerializer,
)
from chatbot.user_info_provider import UserInfoProvider
from chatbot.utils.attachment_format import format_attachment_dtos
from chatbot.utils.exception_utils import wrap_generator_with_exception_handling
from chatbot.utils.file_utils import AttachmentValidationError, extract_attachment
from chatbot.web_conversation_repository import (
    ConversationNotFoundError,
    MessageNotFoundError,
)
from chatbot.web_conversation_service import (
    ForbiddenConversationAccessError,
    ForbiddenMessageAccessError,
    NotLatestMessageError,
)


class MultipartOnlyAutoSchema(SwaggerAutoSchema):
    def get_consumes(self):
        return ["multipart/form-data"]


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
        openapi.Parameter(
            name="assistant_id",
            in_=openapi.IN_FORM,
            type=openapi.TYPE_STRING,
            required=False,
        ),
        openapi.Parameter(
            name="assistant_name",
            in_=openapi.IN_FORM,
            type=openapi.TYPE_STRING,
            required=False,
        ),
        openapi.Parameter(
            name="custom_url",
            in_=openapi.IN_FORM,
            type=openapi.TYPE_STRING,
            required=False,
        ),
        openapi.Parameter(
            name="deployment_id",
            in_=openapi.IN_FORM,
            type=openapi.TYPE_STRING,
            required=False,
        ),
        openapi.Parameter(
            name="deployment_name",
            in_=openapi.IN_FORM,
            type=openapi.TYPE_STRING,
            required=False,
        ),
        openapi.Parameter(
            name="environment",
            in_=openapi.IN_FORM,
            type=openapi.TYPE_STRING,
            required=False,
        ),
        openapi.Parameter(
            name="project_id",
            in_=openapi.IN_FORM,
            type=openapi.TYPE_STRING,
            required=False,
            description="Project UUID to start the chat in. Accepted but not applied yet.",
        ),
    ],
    method="post",
    responses={
        **AUTHORISATION_ERROR_RESPONSES,
        201: ConversationSubmissionResponseSerializer,  # TODO: adapt
    },
    operation_description="Create a new conversation by giving initial user message and streams bot response",
)
@api_view(["POST"])
@authentication_classes([ChatbotAuth])
@permission_classes([IsAuthorizedPole | IsTechnicalUser])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def conversation_stream_post(request):
    # project_id is accepted but not applied yet: the chat lands in the main list.
    request_serializer = StreamedConversationSubmissionRequestSerializer(
        data=request.data
    )
    if not request_serializer.is_valid():
        return Response(request_serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    user_message = request_serializer.validated_data["message"]
    all_documents_included = request_serializer.validated_data["all_documents_included"]
    included_documents = request_serializer.validated_data["included_documents"]
    is_orchestration_delegated = request_serializer.validated_data[
        "is_orchestration_delegated"
    ]
    assistant_id = request_serializer.validated_data.get("assistant_id")
    assistant_name = request_serializer.validated_data.get("assistant_name")
    custom_url = request_serializer.validated_data.get("custom_url")
    deployment_id = request_serializer.validated_data.get("deployment_id")
    deployment_name = request_serializer.validated_data.get("deployment_name")
    environment = request_serializer.validated_data.get("environment")
    locale = request_serializer.validated_data.get("locale")

    injector = Injector(
        ChatbotModule(
            request.logger,
            request.claims["username"],
            request.headers.get("X-Request-ID"),
            is_orchestration_delegated,
            auth_token=getattr(request, "auth_token", None),
            assistant_id=assistant_id,
            assistant_url=custom_url,
        )
    )
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
            return Response({"file": [str(exc)]}, status=status.HTTP_400_BAD_REQUEST)

    factory = injector.get(ConversationFactory)
    service = factory.create_service(ConversationType.WEB)

    web_conversation_dto = service.create_conversation(
        assistant_id=assistant_id,
        assistant_name=assistant_name,
        custom_url=custom_url,
        deployment_id=deployment_id,
        deployment_name=deployment_name,
        environment=environment,
    )

    streamed_response = service.post_message_streamed(
        conversation_uuid=web_conversation_dto.uuid,
        user_message=user_message,
        all_documents_included=all_documents_included,
        entity_id=entity_id,
        included_documents=included_documents,
        request_id=request.headers.get("X-Request-ID"),
        uid=uid,
        attachment=attachment,
    )

    def persist_stopped_send():
        service.save_stopped_exchange(
            web_conversation_dto.uuid, user_message, attachment
        )

    def format_streamed_response(streamed_response):

        def generator():

            # Emmited before the first read so the client learns the conversation id from line one,
            # a stop on the very first message can then find its persisted partial without seeing the 'end' event
            start_serializer = ConversationSubmissionStreamedSerializer(
                data={
                    "event": "start",
                    "payload": {
                        "conversation_id": str(web_conversation_dto.uuid),
                        "conversation_name": web_conversation_dto.name,
                        "conversation_creation_date": web_conversation_dto.creation_date.isoformat(),
                    },
                }
            )
            start_serializer.is_valid(raise_exception=True)
            try:
                yield f"{json.dumps(start_serializer.data, sort_keys=True, default=str)}\n"
            except GeneratorExit:
                persist_stopped_send()
                raise

            for chunk in streamed_response:
                if isinstance(chunk, str):
                    formatted_payload = {"event": "token", "payload": {"token": chunk}}
                elif isinstance(chunk, dict) and chunk.get("type") == "tool_updates":
                    formatted_payload = {
                        "event": chunk.get("type"),
                        "payload": {"token": chunk.get("token")},
                    }
                elif isinstance(chunk, dict) and chunk.get("type") == "reasoning":
                    formatted_payload = {"event": "reasoning", "payload": {}}
                else:

                    formatted_sources = [
                        {
                            "title": source.title,
                            "content": source.content,
                            "source_document": source.source_document,
                            "document_url": source.url,
                            "document_type": source.document_type.value,
                        }
                        for source in chunk.sources
                    ]

                    web_conversation_dto_with_title = (
                        service.generate_title_if_needed(
                            str(web_conversation_dto.uuid),
                            user_message,
                            locale=locale,
                        )
                    )

                    formatted_payload = {
                        "event": "end",
                        "payload": {
                            "sources": formatted_sources,
                            "conversation_name": web_conversation_dto_with_title.name,
                            "all_documents_included": request_serializer.validated_data[
                                "all_documents_included"
                            ],
                            "included_documents": request_serializer.validated_data[
                                "included_documents"
                            ],
                            "conversation_creation_date": web_conversation_dto.creation_date.isoformat(),
                            "conversation_id": str(web_conversation_dto.uuid),
                            "user_message": user_message,
                            "message_id": str(chunk.uuid),
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
                            "inference_caller": Orchestrator.LGP.value,
                            "attachments": format_attachment_dtos(chunk.attachments),
                        },
                    }
                serializer = ConversationSubmissionStreamedSerializer(
                    data=formatted_payload
                )
                serializer.is_valid(raise_exception=True)
                yield f"{json.dumps(serializer.data, sort_keys=True, default=str)}\n"

        return wrap_generator_with_exception_handling(generator, request=request)

    return StreamingHttpResponse(
        format_streamed_response(streamed_response),
        status=status.HTTP_201_CREATED,
        content_type="application/json; charset=utf-8",
    )


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
        openapi.Parameter(
            "replace_message_id",
            openapi.IN_FORM,
            type=openapi.TYPE_STRING,
            required=False,
        ),
    ],
    method="post",
    responses={
        **AUTHORISATION_ERROR_RESPONSES,
        201: MessageResponseSerializer,  # TODO: adapt
    },
    operation_description="Post a new user message and streams bot response",
)
@api_view(["POST"])
@authentication_classes([ChatbotAuth])
@permission_classes([IsAuthorizedPole | IsTechnicalUser])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def message_stream_post(request, conversation_id):
    request_serializer = StreamedMessageSubmissionRequestSerializer(data=request.data)
    if not request_serializer.is_valid():
        return Response(request_serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    user_message = request_serializer.validated_data["message"]
    all_documents_included = request_serializer.validated_data["all_documents_included"]
    included_documents = request_serializer.validated_data["included_documents"]
    is_orchestration_delegated = request_serializer.validated_data[
        "is_orchestration_delegated"
    ]
    replace_message_id = request_serializer.validated_data.get("replace_message_id")
    locale = request_serializer.validated_data.get("locale")

    try:
        conversation = Conversation.objects.select_related("assistant").get(
            uuid=conversation_id
        )
    except Conversation.DoesNotExist as e:
        raise ConversationNotFoundError from e

    assistant = conversation.assistant

    assistant_id = str(assistant.assistant_id) if assistant is not None else None
    assistant_url = assistant.url if assistant is not None else None

    injector = Injector(
        ChatbotModule(
            request.logger,
            request.claims["username"],
            request.headers.get("X-Request-ID"),
            is_orchestration_delegated,
            auth_token=getattr(request, "auth_token", None),
            assistant_id=assistant_id,
            assistant_url=assistant_url,
        )
    )

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
            return Response({"file": [str(exc)]}, status=status.HTTP_400_BAD_REQUEST)

    factory = injector.get(ConversationFactory)
    service = factory.create_service(ConversationType.WEB)
    # TODO: error management

    def persist_stopped_send():
        service.save_stopped_exchange(conversation_id, user_message, attachment)

    if replace_message_id:
        try:
            streamed_response = service.edit_message_streamed(
                message_id=replace_message_id,
                conversation_uuid=conversation_id,
                user_message=user_message,
                all_documents_included=all_documents_included,
                entity_id=entity_id,
                included_documents=included_documents,
                request_id=request.headers.get("X-Request-ID"),
                uid=uid,
                attachment=attachment,
            )

        except (MessageNotFoundError, ConversationNotFoundError):
            return Response(
                {"detail": "Conversation or message not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except (ForbiddenConversationAccessError, ForbiddenMessageAccessError):
            return Response(
                {"detail": "You do not have permission to perform this action."},
                status=status.HTTP_403_FORBIDDEN,
            )
        except NotLatestMessageError:
            return Response(
                {"detail": "Only the newest message in a conversation can be edited."},
                status=status.HTTP_409_CONFLICT,
            )
        except LgpRewindException:
            return Response(
                {
                    "detail": (
                        "Could not rewind the assistant conversation state; "
                        "the original answer was left untouched."
                    )
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )
    else:
        streamed_response = service.post_message_streamed(
            conversation_uuid=conversation_id,
            user_message=user_message,
            all_documents_included=all_documents_included,
            entity_id=entity_id,
            included_documents=included_documents,
            request_id=request.headers.get("X-Request-ID"),
            uid=uid,
            attachment=attachment,
        )

    return StreamingHttpResponse(
        _format_message_stream(
            streamed_response,
            request,
            conversation_id,
            assistant,
            user_message,
            all_documents_included,
            included_documents,
            is_orchestration_delegated,
            is_edited=replace_message_id is not None,
            persist_stopped_send=persist_stopped_send,
            service=service,
            locale=locale,
        ),
        status=status.HTTP_201_CREATED,
        content_type="application/json; charset=utf-8",
    )


def _format_message_stream(
    streamed_response,
    request,
    conversation_id,
    assistant,
    user_message,
    all_documents_included,
    included_documents,
    is_orchestration_delegated,
    is_edited,
    persist_stopped_send,
    service=None,
    locale=None,
):
    def generator():
        start_serializer = MessageSubmissionStreamedSerializer(
            data={
                "event": "start",
                "payload": {"conversation_id": str(conversation_id)},
            }
        )
        start_serializer.is_valid(raise_exception=True)
        try:
            yield f"{json.dumps(start_serializer.data, sort_keys=True, default=str)}\n"
        except GeneratorExit:
            persist_stopped_send()
            raise

        for chunk in streamed_response:
            if isinstance(chunk, str):
                formatted_payload = {"event": "token", "payload": {"token": chunk}}
            elif isinstance(chunk, dict) and chunk.get("type") == "tool_updates":
                formatted_payload = {
                    "event": chunk.get("type"),
                    "payload": {"token": chunk.get("token")},
                }
            elif isinstance(chunk, dict) and chunk.get("type") == "reasoning":
                formatted_payload = {"event": "reasoning", "payload": {}}
            else:
                formatted_sources = [
                    {
                        "title": source.title,
                        "content": source.content,
                        "source_document": source.source_document,
                        "document_url": source.url,
                        "document_type": source.document_type.value,
                    }
                    for source in chunk.sources
                ]

                conversation_name = None
                if service is not None and not is_edited:
                    conversation_dto = service.generate_title_if_needed(
                        conversation_id, user_message, locale=locale
                    )
                    conversation_name = conversation_dto.name

                end_payload = {
                    "sources": formatted_sources,
                    "all_documents_included": all_documents_included,
                    "included_documents": included_documents,
                    "conversation_id": conversation_id,
                    "user_message": user_message,
                    "message_id": str(chunk.uuid),
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
                    "inference_caller": Orchestrator.LGP.value,
                    "attachments": format_attachment_dtos(chunk.attachments),
                    "is_edited": is_edited,
                }

                if conversation_name is not None:
                    end_payload["conversation_name"] = conversation_name

                if assistant is not None:
                    end_payload["assistant_id"] = str(assistant.assistant_id)
                    end_payload["assistant_name"] = assistant.assistant_name
                    end_payload["custom_url"] = assistant.url
                    end_payload["environment"] = assistant.environment
                    end_payload["deployment_id"] = str(assistant.deployment_id)
                    end_payload["deployment_name"] = assistant.deployment_name

                formatted_payload = {"event": "end", "payload": end_payload}

            serializer = MessageSubmissionStreamedSerializer(data=formatted_payload)
            serializer.is_valid(raise_exception=True)
            yield f"{json.dumps(serializer.data, sort_keys=True, default=str)}\n"

    return wrap_generator_with_exception_handling(generator, request=request)
