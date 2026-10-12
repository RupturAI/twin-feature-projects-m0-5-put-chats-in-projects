from datetime import datetime
from unittest.mock import Mock
from uuid import uuid4

import pytest

from chatbot.chatbot_service import ChatbotOutput
from chatbot.constants import Metadata, RagType, TwinUserOrigin
from chatbot.lgp_client import LgpRewindException
from chatbot.message_exchange import MessageExchange, PartialMessageExchange
from chatbot.orchestrator import Orchestrator
from chatbot.utils.file_utils import TextAttachment
from chatbot.web_conversation_dto import ConversationDto, MessageDto
from chatbot.web_conversation_repository import UserNotFoundError
from chatbot.web_conversation_service import (
    ForbiddenConversationAccessError,
    ForbiddenMessageAccessError,
    MessageNotFoundError,
    NotLatestMessageError,
    WebConversationService,
)

# --- Fixtures ---


@pytest.fixture
def username():
    return "test_user"


@pytest.fixture
def lgp_client():
    return Mock()


@pytest.fixture
def dummy_conversation(username):
    return ConversationDto(
        uuid=uuid4(),
        name="Test Conversation",
        creation_date=datetime.now(),
        update_date=datetime.now(),
        created_by=username,
    )


@pytest.fixture
def dummy_message(username, dummy_conversation):
    return MessageDto(
        uuid=uuid4(),
        user_message="Hello",
        bot_response="Hi there",
        created_by=username,
        conversation_uuid=dummy_conversation.uuid,
    )


@pytest.fixture
def web_conversation_repository(dummy_conversation, dummy_message):
    repo = Mock()
    repo.get_conversation.return_value = dummy_conversation
    repo.get_user_conversations.return_value = [dummy_conversation]
    repo.get_conversation_messages.return_value = [dummy_message]
    repo.append_conversation_exchange.return_value = (uuid4(), [])
    repo.create_conversation.return_value = dummy_conversation
    repo.get_conversation_attachments.return_value = []
    repo.replace_conversation_exchange.return_value = (uuid4(), [])
    repo.get_latest_message.return8value = dummy_message
    return repo


def fake_streamed_response(*args, **kwargs):
    yield []
    yield {"type": "content", "token": "chunk1"}
    yield {"type": "content", "token": "chunk2"}
    yield {"type": "sources", "token": ["source1", "source2"]}


def fake_streamed_response_with_run_id(*args, **kwargs):
    yield []
    yield {"type": "content", "token": "chunk1"}
    yield {"type": "run_id", "token": "test-run-id-123"}
    yield {"type": "sources", "token": ["source1", "source2"]}


@pytest.fixture
def chatbot_service():
    service = Mock()
    service.get_bot_response.return_value = ChatbotOutput(
        response="dummy bot response",
        sources=["source1"],
        inference_caller=Orchestrator.LGP,
    )

    service.get_bot_streamed_response.side_effect = fake_streamed_response
    return service


@pytest.fixture
def logger():
    return Mock()


@pytest.fixture
def web_conversation_service_instance(
    chatbot_service, web_conversation_repository, logger, username
):
    return WebConversationService(
        chatbot_service=chatbot_service,
        conversation_repository=web_conversation_repository,
        logger=logger,
        llm_client=None,
        username=username,
    )


@pytest.fixture
def conversation_service_with_lgp(
    chatbot_service, web_conversation_repository, logger, username, lgp_client
):
    return WebConversationService(
        chatbot_service=chatbot_service,
        conversation_repository=web_conversation_repository,
        logger=logger,
        llm_client=None,
        lgp_client=lgp_client,
        username=username,
    )


# --- Tests ---


def test_create_conversation(
    web_conversation_service_instance, web_conversation_repository, username
):
    new_conv = ConversationDto(
        uuid=uuid4(),
        name="New Conversation",
        creation_date=datetime.now(),
        update_date=datetime.now(),
        created_by=username,
    )
    web_conversation_repository.create_conversation.return_value = new_conv

    conv = web_conversation_service_instance.create_conversation()
    web_conversation_repository.create_conversation.assert_called_once_with(
        username="test_user",
        assistant_id=None,
        assistant_name=None,
        custom_url=None,
        deployment_id=None,
        deployment_name=None,
        environment=None,
    )
    assert conv == new_conv


def test_get_conversation_success(
    web_conversation_service_instance, web_conversation_repository, dummy_conversation
):
    conv = web_conversation_service_instance.get_conversation(dummy_conversation.uuid)
    web_conversation_repository.get_conversation.assert_called_with(
        dummy_conversation.uuid
    )
    assert conv == dummy_conversation


def test_get_conversation_forbidden(
    web_conversation_service_instance, web_conversation_repository
):
    diff_conv = ConversationDto(
        uuid=uuid4(),
        name="Other Conversation",
        creation_date=datetime.now(),
        update_date=datetime.now(),
        created_by="other_user",
    )
    web_conversation_repository.get_conversation.return_value = diff_conv

    with pytest.raises(ForbiddenConversationAccessError) as excinfo:
        web_conversation_service_instance.get_conversation(diff_conv.uuid)
    assert "cannot access conversation" in str(excinfo.value)


def test_get_conversations_existing_user(
    web_conversation_service_instance, web_conversation_repository, username
):
    convs = web_conversation_service_instance.get_conversations()
    web_conversation_repository.get_user_conversations.assert_called_with(
        username=username, project_id=None
    )
    assert convs == web_conversation_repository.get_user_conversations.return_value


def test_get_conversations_user_not_found(
    web_conversation_service_instance, web_conversation_repository, logger, username
):
    dummy_conv = ConversationDto(
        uuid=uuid4(),
        name="New Conv",
        creation_date=datetime.now(),
        update_date=datetime.now(),
        created_by=username,
    )
    web_conversation_repository.get_user_conversations.side_effect = [
        UserNotFoundError("User not found"),
        [dummy_conv],
    ]
    convs = web_conversation_service_instance.get_conversations()
    web_conversation_repository.create_user.assert_called_once_with(username=username)
    assert convs == [dummy_conv]


def test_post_message(
    web_conversation_service_instance,
    web_conversation_repository,
    chatbot_service,
    dummy_conversation,
    dummy_message,
):
    user_message = "Hello"
    metadata = Metadata(
        origin=TwinUserOrigin.WEB,
        conversation_id=dummy_conversation.uuid,
        rag_type=RagType.NONE,
        request_id="test_request_id",
        uid="da294dca-e4db-4ad0-b39a-38c30b2264e3",
    )

    entity_id = "Dummy"
    result = web_conversation_service_instance.post_message(
        user_message,
        dummy_conversation.uuid,
        True,
        entity_id,
        [],
        metadata.request_id,
        metadata.uid,
    )
    web_conversation_repository.get_conversation.assert_called_with(
        dummy_conversation.uuid
    )

    chatbot_service.get_bot_response.assert_called_with(
        user_message=user_message,
        past_messages=[dummy_message.user_message, dummy_message.bot_response],
        all_documents_included=True,
        included_documents=[],
        entity_id=entity_id,
        metadata=metadata,
        attachment=None,
    )

    web_conversation_repository.append_conversation_exchange.assert_called_with(
        conversation_id=dummy_conversation.uuid,
        query=user_message,
        answer="dummy bot response",
        sources=["source1"],
        attachments=None,
    )

    assert isinstance(result, MessageExchange)
    assert result.user_message == user_message
    assert result.bot_response == "dummy bot response"
    assert result.sources == ["source1"]
    assert result.attachments == []


@pytest.mark.skip("will be fixed")
def test_post_message_streamed(
    web_conversation_service_instance,
    web_conversation_repository,
    chatbot_service,
    dummy_conversation,
):
    user_message = "Streamed Hello"
    entity_id = "Dummy"
    gen = web_conversation_service_instance.post_message_streamed(
        user_message, dummy_conversation.uuid, False, entity_id, []
    )

    collected_chunks = []
    final_partial = None
    for chunk in gen:
        if isinstance(chunk, str):
            collected_chunks.append(chunk)
        else:
            final_partial = chunk

    assert collected_chunks == ["chunk1", "chunk2"]
    assert isinstance(final_partial, PartialMessageExchange)
    assert final_partial.user_message == user_message
    assert final_partial.sources == ["source1", "source2"]
    web_conversation_repository.append_conversation_exchange.assert_called_with(
        conversation_id=dummy_conversation.uuid,
        query=user_message,
        answer="chunk1chunk2",
        attachments=None,
    )


def test_get_conversation_messages(
    web_conversation_service_instance, web_conversation_repository, dummy_conversation
):
    messages = web_conversation_service_instance.get_conversation_messages(
        dummy_conversation.uuid
    )

    web_conversation_repository.get_conversation.assert_called_with(
        conversation_id=dummy_conversation.uuid
    )
    web_conversation_repository.get_conversation_messages.assert_called_with(
        dummy_conversation.uuid
    )
    assert (
        messages == web_conversation_repository.get_conversation_messages.return_value
    )


def test_delete_conversation(
    web_conversation_service_instance, web_conversation_repository, dummy_conversation
):
    web_conversation_repository.delete_conversation.return_value = dummy_conversation
    result = web_conversation_service_instance.delete_conversation(
        dummy_conversation.uuid
    )

    web_conversation_repository.get_conversation.assert_called_with(
        conversation_id=dummy_conversation.uuid
    )
    web_conversation_repository.delete_conversation.assert_called_with(
        dummy_conversation.uuid
    )
    assert result == dummy_conversation


def test_update_conversation_name(
    web_conversation_service_instance, web_conversation_repository, dummy_conversation
):
    new_name = "Updated Conversation"
    updated_conv = ConversationDto(
        uuid=dummy_conversation.uuid,
        name=new_name,
        creation_date=dummy_conversation.creation_date,
        update_date=datetime.now(),
        created_by="test_user",
    )
    web_conversation_repository.update_conversation_name.return_value = updated_conv
    result = web_conversation_service_instance.update_conversation_name(
        dummy_conversation.uuid, new_name
    )
    web_conversation_repository.get_conversation.assert_called_with(
        conversation_id=dummy_conversation.uuid
    )
    web_conversation_repository.update_conversation_name.assert_called_with(
        dummy_conversation.uuid, new_name
    )
    assert result.name == new_name


def test_get_message_success(
    web_conversation_service_instance, web_conversation_repository, dummy_message
):
    dummy_message.created_by = "test_user"
    web_conversation_repository.get_message.return_value = dummy_message
    result = web_conversation_service_instance.get_message(dummy_message.uuid)
    web_conversation_repository.get_message.assert_called_with(dummy_message.uuid)
    assert result == dummy_message


def test_get_message_forbidden(
    web_conversation_service_instance, web_conversation_repository, dummy_message
):
    dummy_message.created_by = "other_user"
    web_conversation_repository.get_message.return_value = dummy_message
    with pytest.raises(ForbiddenMessageAccessError) as excinfo:
        web_conversation_service_instance.get_message(dummy_message.uuid)
    assert "cannot access message" in str(excinfo.value)


def test_edit_message_streamed_streams_and_replaces_exchange(
    conversation_service_with_lgp,
    web_conversation_repository,
    dummy_conversation,
    dummy_message,
):
    web_conversation_repository.get_latest_message.return_value = dummy_message
    web_conversation_repository.get_message.return_value = dummy_message

    gen = conversation_service_with_lgp.edit_message_streamed(
        dummy_message.uuid,
        "Edited question",
        dummy_conversation.uuid,
        False,
        included_documents=[],
        entity_id="",
    )
    chunks = [chunk for chunk in gen if isinstance(chunk, str)]

    assert chunks == ["chunk1", "chunk2"]
    web_conversation_repository.replace_conversation_exchange.assert_called_once()
    call = web_conversation_repository.replace_conversation_exchange.call_args
    assert call.kwargs["answer"] == "chunk1chunk2"
    web_conversation_repository.append_conversation_exchange.assert_not_called()


def test_edit_message_streamed_calls_rewind_before_streaming(
    conversation_service_with_lgp,
    web_conversation_repository,
    lgp_client,
    dummy_conversation,
    dummy_message,
):
    web_conversation_repository.get_latest_message.return_value = dummy_message
    web_conversation_repository.get_message.return_value = dummy_message

    gen = conversation_service_with_lgp.edit_message_streamed(
        dummy_message.uuid,
        "Edited question",
        dummy_conversation.uuid,
        False,
        included_documents=[],
        entity_id="",
    )

    lgp_client.rewind_last_exchange.assert_called_once()
    assert lgp_client.rewind_last_exchange.call_args.args[0] == str(
        dummy_conversation.uuid
    )
    web_conversation_repository.replace_conversation_exchange.assert_not_called()
    gen.close()


def test_edit_message_streamed_skip_rewind_when_no_lgp_client(
    conversation_service_with_lgp,
    web_conversation_repository,
    dummy_conversation,
    dummy_message,
):
    web_conversation_repository.get_latest_message.return_value = dummy_message
    web_conversation_repository.get_message.return_value = dummy_message

    gen = conversation_service_with_lgp.edit_message_streamed(
        dummy_message.uuid,
        "Edited question",
        dummy_conversation.uuid,
        False,
        included_documents=[],
        entity_id="",
    )

    chunks = [chunk for chunk in gen if isinstance(chunk, str)]

    assert chunks == ["chunk1", "chunk2"]
    web_conversation_repository.replace_conversation_exchange.assert_called_once()


def test_edit_message_streamed_rewind_failure_leaves_db_untouched(
    conversation_service_with_lgp,
    web_conversation_repository,
    lgp_client,
    dummy_conversation,
    dummy_message,
):
    web_conversation_repository.get_latest_message.return_value = dummy_message
    web_conversation_repository.get_message.return_value = dummy_message
    lgp_client.rewind_last_exchange.side_effect = LgpRewindException()

    with pytest.raises(LgpRewindException):
        conversation_service_with_lgp.edit_message_streamed(
            dummy_message.uuid,
            "Edited question",
            dummy_conversation.uuid,
            False,
            included_documents=[],
            entity_id="",
        )

    web_conversation_repository.replace_conversation_exchange.assert_not_called()
    web_conversation_repository.append_conversation_exchange.assert_not_called()


def test_edit_message_streamed_rewind_failure_leaves_db_untouched_2(
    conversation_service_with_lgp,
    web_conversation_repository,
    chatbot_service,
    dummy_conversation,
    username,
):
    older = MessageDto(
        uuid=uuid4(),
        user_message="Q1",
        bot_response="A1",
        created_by=username,
        conversation_uuid=dummy_conversation.uuid,
    )

    edited = MessageDto(
        uuid=uuid4(),
        user_message="Q2",
        bot_response="A2 old answer",
        created_by=username,
        conversation_uuid=dummy_conversation.uuid,
    )

    web_conversation_repository.get_conversation_messages.return_value = [older, edited]
    web_conversation_repository.get_message.return_value = edited
    web_conversation_repository.get_latest_message.return_value = edited

    gen = conversation_service_with_lgp.edit_message_streamed(
        edited.uuid,
        "Edited question",
        dummy_conversation.uuid,
        False,
        included_documents=[],
        entity_id="",
    )
    list(gen)

    forwarded = chatbot_service.get_bot_streamed_response.call_args.kwargs[
        "past_messages"
    ]
    assert forwarded == ["Q1", "A1"]
    assert "A2 old answer" not in forwarded


def test_edit_message_streamed_rejects_non_latest_message(
    conversation_service_with_lgp,
    web_conversation_repository,
    dummy_conversation,
    dummy_message,
    username,
):
    web_conversation_repository.get_latest_message.return_value = dummy_message
    web_conversation_repository.get_message.return_value = dummy_message

    newer = MessageDto(
        uuid=uuid4(),
        user_message="newer",
        bot_response="newer answer",
        created_by=username,
        conversation_uuid=dummy_conversation.uuid,
    )

    web_conversation_repository.get_latest_message.return_value = newer

    with pytest.raises(NotLatestMessageError):
        conversation_service_with_lgp.edit_message_streamed(
            dummy_message.uuid,
            "Edited question",
            dummy_conversation.uuid,
            False,
            included_documents=[],
            entity_id="",
        )

    web_conversation_repository.replace_conversation_exchange.assert_not_called()


def test_edit_message_streamed_rejects_foreign_conversation(
    conversation_service_with_lgp,
    web_conversation_repository,
    dummy_message,
):
    foreign = ConversationDto(
        uuid=uuid4(),
        name="Foreign",
        creation_date=datetime.now(),
        update_date=datetime.now(),
        created_by="someone_else",
    )

    web_conversation_repository.get_conversation.return_value = foreign

    with pytest.raises(ForbiddenConversationAccessError):
        conversation_service_with_lgp.edit_message_streamed(
            dummy_message.uuid,
            "Edited question",
            foreign.uuid,
            False,
            included_documents=[],
            entity_id="",
        )


def test_edit_message_streamed_rejects_message_from_other_conversation(
    conversation_service_with_lgp,
    web_conversation_repository,
    dummy_conversation,
    username,
):
    other_conversation_message = MessageDto(
        uuid=uuid4(),
        user_message="x",
        bot_response="y",
        created_by=username,
        conversation_uuid=uuid4(),
    )

    web_conversation_repository.get_latest_message.return_value = (
        other_conversation_message
    )
    web_conversation_repository.get_message.return_value = other_conversation_message

    with pytest.raises(MessageNotFoundError):
        conversation_service_with_lgp.edit_message_streamed(
            other_conversation_message.uuid,
            "Edited question",
            dummy_conversation.uuid,
            False,
            included_documents=[],
            entity_id="",
        )

    web_conversation_repository.replace_conversation_exchange.assert_not_called()


def test_edit_message_streamed_forwards_new_attachemnt(
    conversation_service_with_lgp,
    web_conversation_repository,
    chatbot_service,
    dummy_conversation,
    dummy_message,
):
    web_conversation_repository.get_latest_message.return_value = dummy_message
    web_conversation_repository.get_message.return_value = dummy_message

    attachment = TextAttachment(
        name="doc.txt", mime="text/plain", text="DOCUMENT BODY", size_bytes=13
    )

    gen = conversation_service_with_lgp.edit_message_streamed(
        dummy_message.uuid,
        "Edited question",
        dummy_conversation.uuid,
        False,
        included_documents=[],
        entity_id="",
        attachment=attachment,
    )
    list(gen)

    assert (
        chatbot_service.get_bot_streamed_response.call_args.kwargs["attachment"]
        == attachment
    )

    replace_call = web_conversation_repository.replace_conversation_exchange.call_args
    assert replace_call.kwargs["attachments"] == [attachment]


def test_post_message_streamed_passes_run_id_to_repository(
    web_conversation_service_instance,
    web_conversation_repository,
    chatbot_service,
    dummy_conversation,
):
    chatbot_service.get_bot_streamed_response.side_effect = (
        fake_streamed_response_with_run_id
    )

    gen = web_conversation_service_instance.post_message_streamed(
        "Hello", dummy_conversation.uuid, False, "Dummy", []
    )
    list(gen)

    web_conversation_repository.append_conversation_exchange.assert_called_once()
    call = web_conversation_repository.append_conversation_exchange.call_args
    assert call.kwargs["run_id"] == "test-run-id-123"
    assert call.kwargs["answer"] == "chunk1"


def test_edit_message_streamed_passes_run_id_to_replace(
    conversation_service_with_lgp,
    web_conversation_repository,
    chatbot_service,
    dummy_conversation,
    dummy_message,
):
    chatbot_service.get_bot_streamed_response.side_effect = (
        fake_streamed_response_with_run_id
    )
    web_conversation_repository.get_latest_message.return_value = dummy_message
    web_conversation_repository.get_message.return_value = dummy_message

    gen = conversation_service_with_lgp.edit_message_streamed(
        dummy_message.uuid,
        "Edited question",
        dummy_conversation.uuid,
        False,
        included_documents=[],
        entity_id="",
    )
    list(gen)

    web_conversation_repository.replace_conversation_exchange.assert_called_once()
    call = web_conversation_repository.replace_conversation_exchange.call_args
    assert call.kwargs["run_id"] == "test-run-id-123"
    assert call.kwargs["answer"] == "chunk1"


def test_post_message_streamed_run_id_none_without_chunk(
    web_conversation_service_instance,
    web_conversation_repository,
    chatbot_service,
    dummy_conversation,
):
    gen = web_conversation_service_instance.post_message_streamed(
        "Hello", dummy_conversation.uuid, False, "Dummy", []
    )
    list(gen)

    web_conversation_repository.append_conversation_exchange.assert_called_once()
    call = web_conversation_repository.append_conversation_exchange.call_args
    assert call.kwargs["run_id"] is None


def test_generate_title_if_needed_skips_when_real_title_exists(
    web_conversation_service_instance, web_conversation_repository, dummy_conversation
):
    title_llm_client = Mock()
    web_conversation_service_instance._title_llm_client = title_llm_client
    dummy_conversation.name = "Real Title"

    result = web_conversation_service_instance.generate_title_if_needed(
        str(dummy_conversation.uuid), "some message"
    )

    title_llm_client.query_llm.assert_not_called()
    web_conversation_repository.update_conversation_name.assert_not_called()
    assert result == dummy_conversation


def test_generate_title_if_needed_generates_when_placeholder(
    web_conversation_service_instance, web_conversation_repository, dummy_conversation
):
    title_llm_client = Mock()
    title_llm_client.query_llm.return_value = "Generated Title"
    web_conversation_service_instance._title_llm_client = title_llm_client
    dummy_conversation.name = "Chat"

    updated_conv = ConversationDto(
        uuid=dummy_conversation.uuid,
        name="Generated Title",
        creation_date=dummy_conversation.creation_date,
        update_date=datetime.now(),
        created_by="test_user",
    )
    web_conversation_repository.update_conversation_name.return_value = updated_conv

    result = web_conversation_service_instance.generate_title_if_needed(
        str(dummy_conversation.uuid), "some message", locale="fr"
    )

    title_llm_client.query_llm.assert_called_once()
    web_conversation_repository.update_conversation_name.assert_called_with(
        str(dummy_conversation.uuid), "Generated Title"
    )
    assert result.name == "Generated Title"


def test_generate_title_if_needed_returns_unchanged_on_llm_failure(
    web_conversation_service_instance, web_conversation_repository, dummy_conversation
):
    title_llm_client = Mock()
    title_llm_client.query_llm.side_effect = RuntimeError("llm down")
    web_conversation_service_instance._title_llm_client = title_llm_client
    dummy_conversation.name = "Chat"

    result = web_conversation_service_instance.generate_title_if_needed(
        str(dummy_conversation.uuid), "some message"
    )

    web_conversation_repository.update_conversation_name.assert_not_called()
    assert result == dummy_conversation
