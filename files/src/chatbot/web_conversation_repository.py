from datetime import datetime
from typing import List, Optional, Tuple
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from chatbot.conversation_repository_protocol import ConversationRepositoryProtocol
from chatbot.models.assistant import Assistant
from chatbot.models.conversation import Conversation
from chatbot.models.message import Message
from chatbot.models.message_attachment import MessageAttachment
from chatbot.models.message_feedback import MessageFeedback
from chatbot.models.user import User
from chatbot.utils.file_utils import Attachment
from chatbot.web_conversation_dto import AttachmentDto, ConversationDto, MessageDto


class ConversationNotFoundError(Exception):
    pass


class MessageNotFoundError(Exception):
    pass


class UserNotFoundError(Exception):
    pass


class WebConversationRepository(ConversationRepositoryProtocol):
    ## Should not be here, business logic?
    def _get_user(self, username: str) -> User:
        try:
            return User.objects.get(username=username)
        except User.DoesNotExist as e:
            raise UserNotFoundError(f"User '{username}' not found") from e

    def create_user(self, username: str) -> None:
        User.objects.create(username=username)

    def _create_web_conversation_dto(
        self, conversation: Conversation
    ) -> ConversationDto:
        assistant = conversation.assistant
        return ConversationDto(
            uuid=conversation.uuid,
            name=conversation.name,
            creation_date=conversation.created_at,
            update_date=conversation.updated_at,
            created_by=str(conversation.created_by),
            pinned=conversation.pinned,
            pinned_at=conversation.pinned_at,
            assistant_id=str(assistant.assistant_id) if assistant else None,
            assistant_name=str(assistant.assistant_name) if assistant else None,
            custom_url=assistant.url if assistant else None,
            deployment_id=str(assistant.deployment_id) if assistant else None,
            deployment_name=str(assistant.deployment_name) if assistant else None,
            environment=assistant.environment if assistant else None,
            project_id=conversation.project_id,
        )

    def _create_message_dto(self, message: Message) -> MessageDto:
        return MessageDto(
            uuid=message.uuid,
            user_message=message.user_message,
            bot_response=message.bot_response,
            sources=message.sources or [],
            created_by=str(message.conversation.created_by),
            attachments=[
                self._create_attachment_dto(attachment, message_uuid=message.uuid)
                for attachment in message.attachments.all()
            ],
            conversation_uuid=message.conversation.uuid,
            run_id=getattr(message, "run_id", None),
        )

    def _create_attachment_dto(
        self, attachment: MessageAttachment, message_uuid: UUID
    ) -> AttachmentDto:
        return AttachmentDto(
            uuid=attachment.uuid,
            message_uuid=message_uuid,
            name=attachment.name,
            mime=attachment.mime,
            kind=attachment.kind,
            size_bytes=attachment.size_bytes,
            created_at=attachment.created_at,
        )

    def update_conversation_name(
        self, conversation_id: str, new_name: str
    ) -> ConversationDto:
        try:
            conversation = Conversation.objects.get(uuid=conversation_id)
        except Conversation.DoesNotExist as e:
            raise ConversationNotFoundError from e

        conversation.name = new_name
        conversation.save()

        return self._create_web_conversation_dto(conversation)

    def set_conversation_pinned(
        self, conversation_id: str, pinned: bool
    ) -> ConversationDto:
        try:
            conversation = Conversation.objects.get(uuid=conversation_id)
        except Conversation.DoesNotExist as e:
            raise ConversationNotFoundError from e

        fields = {"pinned": pinned}
        if not pinned:
            fields["pinned_at"] = None
        elif conversation.pinned_at is None:
            fields["pinned_at"] = timezone.now()

        updated_fields = []
        for field, value in fields.items():
            if hasattr(conversation, field):
                setattr(conversation, field, value)
                updated_fields.append(field)

        conversation.save(update_fields=updated_fields)

        return self._create_web_conversation_dto(conversation)

    def move_conversation_to_project(
        self, conversation_id: str, project_id: UUID
    ) -> ConversationDto:
        try:
            conversation = Conversation.objects.get(uuid=conversation_id)
        except Conversation.DoesNotExist as e:
            raise ConversationNotFoundError from e

        conversation.project_id = project_id
        conversation.save(update_fields=["project"])

        return self._create_web_conversation_dto(conversation)

    def delete_conversation(self, conversation_id: str) -> ConversationDto:
        try:
            conversation = Conversation.objects.get(uuid=conversation_id)
        except Conversation.DoesNotExist as e:
            raise ConversationNotFoundError from e
        dto = self._create_web_conversation_dto(conversation)
        conversation.delete()
        return dto

    def get_user_conversations(
        self, username: str, project_id: Optional[UUID] = None
    ) -> List[ConversationDto]:
        user = self._get_user(username)
        conversations = (
            Conversation.objects.filter(created_by=user, project_id=project_id)
            .select_related("assistant")
            .order_by("-pinned", "-pinned_at", "-updated_at")
        )
        formatted_conversations = []
        for conversation in conversations:
            dto = self._create_web_conversation_dto(conversation=conversation)
            formatted_conversations.append(dto)
        return formatted_conversations

    def get_conversation(self, conversation_id):
        try:
            conversation = Conversation.objects.get(uuid=conversation_id)
        except Conversation.DoesNotExist as e:
            raise ConversationNotFoundError from e

        return self._create_web_conversation_dto(conversation)

    def create_conversation(
        self,
        username: str,
        assistant_id: str = None,
        assistant_name: str = None,
        custom_url: str = None,
        deployment_id: str = None,
        deployment_name: str = None,
        environment: str = None,
        project_id: Optional[UUID] = None,
    ) -> ConversationDto:
        creation_date = datetime.now()
        user = self._get_user(username)

        assistant = None

        if (
            assistant_id
            and assistant_name
            and deployment_id
            and deployment_name
            and custom_url
        ):
            assistant, created = Assistant.objects.get_or_create(
                assistant_id=assistant_id,
                deployment_id=deployment_id,
                url=custom_url,
                defaults={
                    "assistant_name": assistant_name,
                    "deployment_name": deployment_name,
                    "environment": environment,
                },
            )

            update_fields = []

            if assistant_name and assistant.assistant_name != assistant_name:
                assistant.assistant_name = assistant_name
                update_fields.append("assistant_name")

            if deployment_name and assistant.deployment_name != deployment_name:
                assistant.deployment_name = deployment_name
                update_fields.append("deployment_name")

            if environment and assistant.environment != environment:
                assistant.environment = environment
                update_fields.append("environment")

            if update_fields:
                update_fields.append("updated_at")
                assistant.save(update_fields=update_fields)

        conversation = Conversation.objects.create(
            name="Chat",
            created_at=creation_date,
            updated_at=creation_date,
            created_by=user,
            assistant=assistant,
            project_id=project_id,
        )

        return self._create_web_conversation_dto(conversation)

    def get_conversation_messages(self, conversation_id: str) -> List[MessageDto]:
        messages = list(
            Message.get_conversation_messages(conversation_uuid=conversation_id)
        )
        return [self._create_message_dto(message) for message in messages]

    def _create_exchange(
        self,
        conversation: Conversation,
        query: str,
        answer: str,
        sources: Optional[List] = None,
        attachments: Optional[List[Attachment]] = None,
        run_id: Optional[str] = None,
    ) -> Tuple[Message, List[MessageAttachment]]:

        sources = sources or []

        serialized_sources = [
            {
                "chunk_id": chunk.id,
                "title": chunk.title,
                "source_document": chunk.source_document,
                "document_url": chunk.url,
                "document_type": chunk.document_type.value,
            }
            for chunk in sources
        ]

        message = Message.objects.create(
            conversation=conversation,
            user_message=query,
            bot_response=answer,
            sources=serialized_sources,
            run_id=run_id,
        )

        persisted_attachments = [
            MessageAttachment.objects.create(
                message=message,
                name=attachment.name,
                mime=attachment.mime,
                kind=attachment.kind,
                size_bytes=attachment.size_bytes,
            )
            for attachment in (attachments or [])
        ]

        conversation.updated_at = timezone.now()
        conversation.save()

        return message, persisted_attachments

    def append_conversation_exchange(
        self,
        conversation_id: str,
        query: str,
        answer: str,
        sources: Optional[List] = None,
        attachments: Optional[List[Attachment]] = None,
        run_id: Optional[str] = None,
    ) -> Tuple[UUID, List[AttachmentDto]]:
        with transaction.atomic():
            conversation = Conversation.objects.get(uuid=conversation_id)
            message, persisted_attachments = self._create_exchange(
                conversation, query, answer, sources, attachments, run_id=run_id
            )

        attachment_dtos = [
            self._create_attachment_dto(attachment, message_uuid=message.uuid)
            for attachment in persisted_attachments
        ]

        return message.uuid, attachment_dtos

    def replace_conversation_exchange(
        self,
        conversation_id: str,
        replace_message_id: str,
        query: str,
        answer: str,
        sources: Optional[List] = None,
        attachments: Optional[List[Attachment]] = None,
        run_id: Optional[str] = None,
    ) -> Tuple[UUID, List[AttachmentDto]]:
        with transaction.atomic():
            conversation = Conversation.objects.get(uuid=conversation_id)

            old_rows = Message.objects.filter(
                uuid=replace_message_id, conversation=conversation
            )
            feedback_id = old_rows.values_list("feedback_id", flat=True).first()
            old_rows.delete()
            if feedback_id is not None:
                MessageFeedback.objects.filter(pk=feedback_id).delete()

            message, persisted_attachments = self._create_exchange(
                conversation, query, answer, sources, attachments, run_id=run_id
            )

        attachment_dtos = [
            self._create_attachment_dto(attachment, message_uuid=message.uuid)
            for attachment in persisted_attachments
        ]

        return message.uuid, attachment_dtos

    def get_conversation_attachments(self, conversation_id: str) -> list[AttachmentDto]:
        try:
            conversation = Conversation.objects.get(uuid=conversation_id)
        except Conversation.DoesNotExist as e:
            raise ConversationNotFoundError from e

        attachments = (
            MessageAttachment.objects.filter(message__conversation=conversation)
            .select_related("message")
            .order_by("created_at")
        )

        return [
            self._create_attachment_dto(
                attachment, message_uuid=attachment.message.uuid
            )
            for attachment in attachments
        ]

    def get_message(self, message_id: str) -> MessageDto:
        try:
            message_exchange = Message.objects.get(uuid=message_id)
        except Message.DoesNotExist as e:
            raise MessageNotFoundError from e
        return self._create_message_dto(message_exchange)

    def get_latest_message(self, conversation_id: str) -> Optional[MessageDto]:
        try:
            Conversation.objects.get(uuid=conversation_id)
        except Conversation.DoesNotExist as e:
            raise ConversationNotFoundError from e

        message = (
            Message.objects.filter(conversation__uuid=conversation_id)
            .order_by("-created_at", "-id")
            .first()
        )

        if message is None:
            return None

        return self._create_message_dto(message)
