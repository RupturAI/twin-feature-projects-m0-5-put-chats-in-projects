from django.conf import settings
from rest_framework import serializers
from rest_framework.serializers import (
    BooleanField,
    CharField,
    ChoiceField,
    DateTimeField,
    DictField,
    FileField,
    IntegerField,
    JSONField,
    ListField,
    ModelSerializer,
    Serializer,
    URLField,
    UUIDField,
    ValidationError,
)

from chatbot.enums import DocumentType

from .models.info_message import InfoMessage


class SourceSerializer(Serializer):
    title = CharField(max_length=10000)
    content = CharField(max_length=300000)
    source_document = CharField(max_length=1000)
    document_url = CharField(max_length=1000, allow_null=True)
    document_type = CharField(max_length=100)


class MessageAttachmentResponseSerializer(Serializer):
    id = CharField(max_length=100)
    message_id = CharField(max_length=100)
    name = CharField(max_length=255)
    mime = CharField(max_length=127)
    kind = CharField(max_length=16)
    size_bytes = IntegerField(min_value=0)
    created_at = DateTimeField()


user_message_field = CharField(
    help_text="User message", max_length=settings.USER_MESSAGE_MAXIMUM_SIZE
)
document_id_field = CharField(max_length=1000)
sources_field = SourceSerializer(
    many=True,
    allow_empty=True,
    max_length=20,
)
included_documents_field = ListField(
    allow_null=True,
    default=[],
    allow_empty=True,
    child=CharField(max_length=1000),
    max_length=20,
)
all_documents_included_field = BooleanField(default=False)
attachments_field = MessageAttachmentResponseSerializer(
    many=True, allow_empty=True, default=list
)


class MessageSubmissionSerializer(Serializer):
    message = user_message_field
    conversation_history = CharField(
        allow_blank=True,
        help_text="Past messages exchanged between user and bot.",
        required=False,
    )


class ConversationPatchRequestSerializer(Serializer):
    name = CharField(max_length=200, required=False)
    pinned = BooleanField(required=False)
    project_id = CharField(
        max_length=200,
        required=False,
        help_text="Project UUID to move the chat into. Accepted but not applied yet.",
    )

    def validate(self, data):
        provided = [
            field for field in ("name", "pinned", "project_id") if field in data
        ]
        if len(provided) != 1:
            raise ValidationError(
                "Provide exactly one of 'name', 'pinned' or 'project_id'."
            )
        return data


class ConversationListQuerySerializer(Serializer):
    project_id = CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        help_text="Project UUID: list this project's chats instead of the main list.",
    )


class ConversationMultipleDeletionRequestSerializer(Serializer):
    conversation_ids = ListField(
        child=CharField(max_length=200),
        allow_empty=False,
    )


class ConversationMultipleDeletionResponseSerializer(Serializer):
    deleted = ListField(
        child=CharField(max_length=200),
    )
    errors = DictField(
        child=CharField(),
    )


class ConversationResponseSerializer(Serializer):
    name = CharField(max_length=200)
    created_at = DateTimeField()
    updated_at = DateTimeField()
    id = CharField(max_length=200)
    pinned = BooleanField(required=False, default=False)
    pinned_at = DateTimeField(required=False, allow_null=True)
    attachments = attachments_field
    assistant_id = CharField(max_length=255, allow_null=True, required=False)
    assistant_name = CharField(max_length=255, allow_null=True, required=False)
    custom_url = URLField(max_length=2048, allow_null=True, required=False)
    deployment_id = CharField(max_length=255, allow_null=True, required=False)
    deployment_name = CharField(max_length=255, allow_null=True, required=False)
    environment = CharField(max_length=64, allow_null=True, required=False)
    project_id = UUIDField(allow_null=True, required=False)


class MyAssistantResponseSerializer(Serializer):
    id = CharField(max_length=200)
    name = CharField(max_length=200)
    description = CharField(source="short_description", read_only=True)
    updated_at = DateTimeField(required=False, allow_null=True)


class MyAssistantDeploymentResponseSerializer(Serializer):
    id = CharField(max_length=200)
    name = CharField(max_length=200)
    custom_url = CharField(max_length=1000)
    assistants = MyAssistantResponseSerializer(many=True)


class MyAssistantWorkspaceResponseSerializer(Serializer):
    id = CharField(max_length=200)
    name = CharField(max_length=200)
    deployments = MyAssistantDeploymentResponseSerializer(many=True)


class MyAssistantsResponseSerializer(Serializer):
    environment = CharField(max_length=200)
    workspaces = MyAssistantWorkspaceResponseSerializer(many=True)


class AssistantListResponseSerializer(Serializer):
    custom_url = CharField(max_length=1000)
    assistant_id = CharField(max_length=200)
    deployment_id = CharField(max_length=200)
    name = CharField(max_length=200)
    graph_id = CharField(max_length=200)


class UserInfoResponseSerializer(Serializer):
    username = CharField(max_length=255)
    api_key_id = CharField(max_length=150, allow_blank=True, allow_null=True)
    is_technical = BooleanField()
    is_beta_user = BooleanField()


class DisclaimerResponseSerializer(Serializer):
    content = CharField(allow_blank=True)
    id = CharField(allow_null=True)
    accepted = BooleanField()
    accepted_at = DateTimeField(allow_null=True)


class DisclaimerAcceptRequestSerializer(Serializer):
    accepted = BooleanField(required=True)

    def validate(self, data):
        if data.get("accepted") is not True:
            raise ValidationError("Field 'accepted' must be true.")
        return data


class ConversationSubmissionResponseSerializer(Serializer):
    user_message = user_message_field
    conversation_name = CharField(max_length=200)
    conversation_creation_date = DateTimeField()
    conversation_id = CharField(max_length=200)
    message_id = CharField(max_length=1000)
    bot_response = CharField(max_length=1000)
    all_documents_included = BooleanField()
    sources = sources_field
    included_documents = included_documents_field
    is_orchestration_delegated = BooleanField(required=False, default=False)
    required_orchestrator = CharField(required=False)
    actual_orchestrator = CharField(required=False)
    inference_caller = CharField(required=False)
    attachments = attachments_field


class ConversationDeletionResponseSerializer(Serializer):
    conversation_id = CharField(max_length=1000)
    bot_response = CharField(max_length=1000)
    creation_date = DateTimeField()
    title = CharField(max_length=200)


class MessageResponseSerializer(Serializer):
    message_id = CharField(max_length=1000)
    user_message = user_message_field
    bot_response = CharField(max_length=1000)
    sources = sources_field
    all_documents_included = all_documents_included_field
    included_documents = included_documents_field
    required_orchestrator = CharField(required=False)
    actual_orchestrator = CharField(required=False)
    inference_caller = CharField(required=False)
    attachments = attachments_field
    assistant_id = CharField(allow_null=True, required=False, allow_blank=True)
    assistant_name = CharField(allow_null=True, required=False, allow_blank=True)
    custom_url = URLField(allow_null=True, required=False)
    environment = CharField(allow_blank=True, allow_null=True, required=False)
    deployment_id = CharField(allow_blank=True, allow_null=True, required=False)
    deployment_name = CharField(allow_blank=True, allow_null=True, required=False)


class MessageSubmissionRequestSerializer(Serializer):
    message = user_message_field
    all_documents_included = all_documents_included_field
    included_documents = included_documents_field
    is_orchestration_delegated = BooleanField(required=False, default=False)
    file = FileField(required=False, allow_empty_file=True, use_url=False)

    assistant_id = CharField(required=False, allow_blank=True, allow_null=True)
    assistant_name = CharField(required=False, allow_blank=True, allow_null=True)

    custom_url = URLField(required=False, allow_blank=True, allow_null=True)

    deployment_id = CharField(required=False, allow_blank=True, allow_null=True)
    deployment_name = CharField(required=False, allow_blank=True, allow_null=True)

    environment = CharField(required=False, allow_blank=True, allow_null=True)

    locale = CharField(required=False, allow_blank=True, allow_null=True)

    def validate(self, attrs):
        assistant_fields = [
            "assistant_id",
            "assistant_name",
            "custom_url",
            "deployment_id",
            "deployment_name",
            "environment",
        ]

        provided_fields = [
            field for field in assistant_fields if attrs.get(field) not in (None, "")
        ]

        if provided_fields and len(provided_fields) != len(assistant_fields):
            raise ValidationError(
                "assistant_id, assistant_name, custom_url, deployment_id, deployment_name and environment must be provided together."
            )

        return attrs


class StreamedConversationSubmissionRequestSerializer(
    MessageSubmissionRequestSerializer
):
    project_id = CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        help_text="Project UUID to start the chat in. Accepted but not applied yet.",
    )


class StreamedMessageSubmissionRequestSerializer(MessageSubmissionRequestSerializer):
    replace_message_id = CharField(max_length=1000, required=False)


class FeedbackCorrectionSerializer(Serializer):
    key_facts = ListField(
        child=CharField(max_length=1000),
        required=False,
        allow_empty=True,
        allow_null=True,
        help_text="Corrected facts that should replace the wrong ones in the answer.",
    )
    key_elements = ListField(
        child=CharField(max_length=1000),
        required=False,
        allow_empty=True,
        allow_null=True,
        help_text="The elements of the answer that were wrong and need correcting.",
    )


class FeedbackSerializer(Serializer):
    key = ChoiceField(
        choices=[
            ("user_score", "user_score - 1=thumbs up, 0=thumbs down"),
            ("hallucination", "hallucination - 1=detected, 0=none"),
            ("completeness", "completeness - 1=complete, 0=incomplete"),
            ("trajectory", "trajectory - 1=good, 0=bad"),
        ],
        help_text=(
            "Feedback dimension. One LangSmith record is created per key. "
            "Note: hallucination=1 is a failure flag whose polarity is inverted "
            "relative to user_score, completeness and trajectory."
        ),
    )
    score = ChoiceField(
        choices=[(0, "0"), (1, "1")],
        help_text="Binary score. For hallucination, 1 means a failure (detected).",
    )
    comment = CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
        help_text="Optional free-text comment about this feedback dimension.",
    )
    correction = FeedbackCorrectionSerializer(
        required=False,
        allow_null=True,
        help_text=(
            "Optional structured correction, used for bad "
            "hallucination/completeness scores."
        ),
    )


class FeedbackBatchRequestSerializer(Serializer):
    feedbacks = ListField(
        child=FeedbackSerializer(),
        allow_empty=False,
    )


class FeedbackBatchResponseSerializer(Serializer):
    submitted = ListField(child=IntegerField())
    errors = DictField(child=CharField())


class DocumentSubmissionRequestSerializer(Serializer):
    file = FileField(max_length=100000, use_url=False)


class DocumentResponseSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=128)
    document_type = serializers.ChoiceField(
        choices=[(e.value, e.value) for e in DocumentType]
    )


class DocumentDeletionRequestSerializer(Serializer):
    document_id = CharField(max_length=1000)


class InfoMessageSerializer(ModelSerializer):
    class Meta:
        model = InfoMessage
        fields = ["message", "severity", "expires_at"]


class TokenPayloadSerializer(Serializer):
    token = CharField(allow_blank=True, trim_whitespace=False)


class ConversationSubmissionStartPayloadSerializer(Serializer):
    conversation_id = CharField(max_length=200)
    conversation_name = CharField(max_length=200)
    conversation_creation_date = CharField()


class MessageSubmissionStartPayloadSerializer(Serializer):
    conversation_id = CharField(max_length=200)


class ConversationSubmissionEndPayloadSerializer(Serializer):
    user_message = user_message_field
    conversation_creation_date = CharField()
    conversation_id = CharField(max_length=200)
    message_id = CharField(max_length=1000)
    sources = sources_field
    all_documents_included = BooleanField()
    included_documents = included_documents_field
    attachments = attachments_field


class MessageSubmissionEndPayloadSerializer(Serializer):
    user_message = user_message_field
    conversation_name = CharField(max_length=200, required=False, allow_blank=True)
    message_id = CharField(max_length=1000)
    sources = sources_field
    all_documents_included = BooleanField()
    included_documents = included_documents_field
    attachments = attachments_field
    is_edited = BooleanField(default=False)
    assistant_id = CharField(required=False, allow_blank=True, allow_null=True)
    assistant_name = CharField(required=False, allow_blank=True, allow_null=True)
    custom_url = URLField(required=False, allow_blank=True, allow_null=True)
    deployment_id = CharField(required=False, allow_blank=True, allow_null=True)
    deployment_name = CharField(required=False, allow_blank=True, allow_null=True)
    environment = CharField(required=False, allow_blank=True, allow_null=True)


class ToolFunctionSerializer(Serializer):
    name = CharField()
    arguments = CharField()


class ToolCallSerializer(Serializer):
    id = CharField(required=False, allow_null=True)
    type = CharField(required=False)
    index = IntegerField(required=False)
    function = ToolFunctionSerializer(required=False)
    args = JSONField(required=False)


class MessageChunkSerializer(Serializer):
    content = CharField(required=False, allow_blank=True)
    type = CharField()
    name = CharField(required=False, allow_null=True)
    id = CharField(required=False)

    additional_kwargs = DictField(required=False)
    response_metadata = DictField(required=False)

    tool_calls = ListField(child=ToolCallSerializer(), required=False)

    invalid_tool_calls = ListField(child=DictField(), required=False)

    tool_call_chunks = ListField(child=ToolCallSerializer(), required=False)

    usage_metadata = DictField(required=False, allow_null=True)
    chunk_position = JSONField(required=False, allow_null=True)


class MessageMetaSerializer(Serializer):
    graph_id = CharField(required=False)
    created_by = CharField(required=False)

    assistant_id = CharField(required=False)
    run_attempt = IntegerField(required=False)

    langgraph_version = CharField(required=False)
    langgraph_api_version = CharField(required=False)

    langgraph_node = CharField(required=False)
    langgraph_triggers = ListField(child=CharField(), required=False)

    run_id = CharField(required=False)
    thread_id = CharField(required=False)

    ls_provider = CharField(required=False)
    ls_model_name = CharField(required=False)


class MessagesPayloadSerializer(Serializer):
    token = ListField(child=DictField())

    def validate(self, data):
        token = data.get("token")

        if not isinstance(token, (list, tuple)) or len(token) != 2:
            raise ValidationError("token must be a list/tuple of 2 elements")

        message_part = token[0]
        meta_part = token[1]

        MessageChunkSerializer(data=message_part).is_valid(raise_exception=True)
        MessageMetaSerializer(data=meta_part).is_valid(raise_exception=True)

        return data


class ValueMessageSerializer(Serializer):
    content = CharField(required=False, allow_blank=True, allow_null=True)
    additional_kwargs = DictField(required=False)
    response_metadata = DictField(required=False)
    type = CharField()
    name = CharField(required=False, allow_null=True)
    id = CharField(required=False, allow_null=True)

    # Pour les messages AI / tool
    tool_calls = ListField(required=False)
    invalid_tool_calls = ListField(required=False)
    usage_metadata = JSONField(required=False, allow_null=True)
    tool_call_id = CharField(required=False, allow_null=True)
    artifact = JSONField(required=False, allow_null=True)
    status = CharField(required=False, allow_null=True)


class ValueTokenSerializer(Serializer):
    messages = ValueMessageSerializer(many=True)


class ValuesPayloadSerializer(Serializer):
    token = ValueTokenSerializer()


class UpdatesPayloadSerializer(Serializer):
    token = DictField()


class TaskMessageSerializer(Serializer):
    content = CharField(required=False, allow_blank=True)
    additional_kwargs = DictField(required=False)
    response_metadata = DictField(required=False)
    type = CharField(required=False)
    name = CharField(required=False, allow_null=True)
    id = CharField(required=True)


class TaskTokenSerializer(Serializer):
    id = CharField()
    name = CharField()

    input = JSONField(required=False)

    triggers = ListField(child=CharField(), required=False)


class TaskInputSerializer(Serializer):
    # messages = ListField(child=TaskMessageSerializer(), required=False)
    messages = TaskMessageSerializer(many=True, required=False)


class TasksPayloadSerializer(Serializer):
    token = TaskTokenSerializer()


class ToolUpdatesSerializer(Serializer):
    name = CharField()
    subagent_type = CharField()
    description = CharField(required=False, allow_blank=True)
    type = CharField()
    status = CharField(required=False, allow_blank=True)


class ToolUpdatesPayloadSerializer(Serializer):
    token = DictField(child=ToolUpdatesSerializer())


class ReasoningPayloadSerializer(Serializer):
    pass


class ConversationSubmissionStreamedSerializer(Serializer):
    event = CharField()
    payload = DictField()

    def validate(self, data):
        event = data.get("event")
        payload = data.get("payload")

        if event == "start":
            sub_serializer = ConversationSubmissionStartPayloadSerializer(data=payload)
        elif event == "token":
            sub_serializer = TokenPayloadSerializer(data=payload)
        elif event == "end":
            sub_serializer = ConversationSubmissionEndPayloadSerializer(data=payload)
        elif event == "messages":
            sub_serializer = MessagesPayloadSerializer(data=payload)
        elif event == "values":
            sub_serializer = ValuesPayloadSerializer(data=payload)
        elif event == "tasks":
            sub_serializer = TasksPayloadSerializer(data=payload)
        elif event == "updates":
            sub_serializer = UpdatesPayloadSerializer(data=payload)
        elif event == "tool_updates":
            sub_serializer = ToolUpdatesPayloadSerializer(data=payload)
        elif event == "reasoning":
            sub_serializer = ReasoningPayloadSerializer(data=payload)
        else:
            raise ValidationError(f"Unknown event type: {event}")

        sub_serializer.is_valid(raise_exception=True)
        data["payload"] = sub_serializer.validated_data
        return data


class MessageSubmissionStreamedSerializer(Serializer):
    event = CharField()
    payload = DictField()

    def validate(self, data):
        event = data.get("event")
        payload = data.get("payload")

        if event == "start":
            sub_serializer = MessageSubmissionStartPayloadSerializer(data=payload)
        elif event == "token":
            sub_serializer = TokenPayloadSerializer(data=payload)
        elif event == "end":
            sub_serializer = MessageSubmissionEndPayloadSerializer(data=payload)
        elif event == "messages":
            sub_serializer = MessagesPayloadSerializer(data=payload)
        elif event == "values":
            sub_serializer = ValuesPayloadSerializer(data=payload)
        elif event == "tasks":
            sub_serializer = TasksPayloadSerializer(data=payload)
        elif event == "updates":
            sub_serializer = UpdatesPayloadSerializer(data=payload)
        elif event == "tool_updates":
            sub_serializer = ToolUpdatesPayloadSerializer(data=payload)
        elif event == "reasoning":
            sub_serializer = ReasoningPayloadSerializer(data=payload)
        else:
            raise ValidationError(f"Unknown event type: {event}")

        sub_serializer.is_valid(raise_exception=True)
        data["payload"] = sub_serializer.validated_data
        return data


class ChannelDataSerializer(Serializer):
    clientActivityID = CharField(required=False, allow_null=True)
    clientTimestamp = DateTimeField(required=False, allow_null=True)


class EntitySerializer(Serializer):
    requiresBotState = BooleanField(required=False)
    supportsListening = BooleanField(required=False)
    supportsTts = BooleanField(required=False)
    type = CharField()


class MemberSerializer(Serializer):
    id = CharField()
    name = CharField()


class ConversationSerializer(Serializer):
    id = CharField()


class ParticipantSerializer(Serializer):
    id = CharField()
    name = CharField()
    role = CharField(required=False, allow_null=True)


class TeamsActivityReceivedSerializer(Serializer):
    id = CharField()
    type = CharField()
    channelId = CharField()
    conversation = ConversationSerializer()
    localTimestamp = DateTimeField()
    timestamp = DateTimeField()
    recipient = ParticipantSerializer()
    from_field = ParticipantSerializer(source="from")
    locale = CharField(required=False, allow_null=True)
    serviceUrl = URLField()

    channelData = ChannelDataSerializer(required=False)
    text = CharField(required=False, allow_null=True)
    textFormat = CharField(required=False, allow_null=True)
    entities = EntitySerializer(many=True, required=False)

    membersAdded = MemberSerializer(many=True, required=False)
    membersRemoved = MemberSerializer(many=True, required=False)


class AttachmentResponseSerializer(Serializer):
    id = CharField()
    filename = CharField()
    file_type = CharField()
    size = IntegerField()
    status = CharField()
    uploaded_at = DateTimeField()


class AttachmentUploadResponseSerializer(Serializer):
    attachments = ListField(child=AttachmentResponseSerializer())


class AttachmentUploadRequestSerializer(Serializer):
    file = FileField(required=True)


# ---- Projects ----


class ProjectCreateRequestSerializer(Serializer):
    name = CharField(max_length=200)
    description = CharField(max_length=500, required=False, allow_blank=True)


class ProjectPatchRequestSerializer(Serializer):
    name = CharField(max_length=200, required=False)
    description = CharField(max_length=500, required=False, allow_blank=True)

    def validate(self, data: dict) -> dict:
        if "name" not in data and "description" not in data:
            raise ValidationError("Provide at least one of 'name' or 'description'.")
        return data


class ProjectFileResponseSerializer(Serializer):
    id = CharField(max_length=200)
    name = CharField(max_length=255)
    file_type = CharField(max_length=127)
    size_bytes = IntegerField(min_value=0)
    created_at = DateTimeField()


class ProjectResponseSerializer(Serializer):
    id = CharField(max_length=200)
    name = CharField(max_length=200)
    description = CharField(max_length=500, allow_blank=True)
    created_at = DateTimeField()
    updated_at = DateTimeField()


class ProjectDetailResponseSerializer(ProjectResponseSerializer):
    conversation_count = IntegerField(min_value=0)
    files = ProjectFileResponseSerializer(many=True)
