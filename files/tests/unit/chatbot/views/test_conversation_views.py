from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from django.test import RequestFactory
from django.urls import resolve
from rest_framework import status

from chatbot.authentication import ChatbotAuth, IsAuthorizedPole
from chatbot.models import Conversation, Project
from chatbot.models.user import User
from chatbot.project_service import ForbiddenProjectAccessError
from chatbot.web_conversation_repository import WebConversationRepository
from chatbot.web_conversation_service import (
    ForbiddenConversationAccessError,
    WebConversationService,
)

USERNAME = "test.user@test.com"
CONVERSATIONS_PATH = "/chatbot/conversations"
STREAMED_CONVERSATIONS_PATH = "/chatbot/streaming/conversations"
UNKNOWN_PROJECT_IDS = ["3f1c2a4e-9b7d-4c1e-8a2f-5d6e7f8a9b0c", "not-a-uuid"]


@pytest.fixture
def bypass_authentication():
    with patch.object(
        ChatbotAuth, "authenticate", return_value=(MagicMock(), None)
    ), patch.object(IsAuthorizedPole, "has_permission", return_value=True):
        yield


@pytest.fixture
def conversation_service():
    service = WebConversationService(
        chatbot_service=MagicMock(),
        conversation_repository=WebConversationRepository(),
        llm_client=MagicMock(),
        logger=MagicMock(),
        username=USERNAME,
    )
    with patch("chatbot.views.chatbot_views.Injector") as injector, patch(
        "chatbot.views.stream_views.Injector"
    ) as stream_injector:
        injector.return_value.get.return_value.create_service.return_value = service
        stream_injector.return_value.get.return_value.create_service.return_value = (
            service
        )
        yield service


@pytest.fixture
def me():
    return User.objects.create(username=USERNAME)


@pytest.fixture
def my_project(me):
    return Project.objects.create(name="Q3 audit", created_by=me)


@pytest.fixture
def someone_elses_project():
    someone_else = User.objects.create(username="someone.else@test.com")
    return Project.objects.create(name="Not mine", created_by=someone_else)


def _send(method, path, body=None, **factory_kwargs):
    if body is not None:
        factory_kwargs.update(data=body, content_type="application/json")
    request = getattr(RequestFactory(), method)(path, **factory_kwargs)
    request.logger = MagicMock()
    request.claims = {"username": USERNAME}
    match = resolve(path)
    return match.func(request, **match.kwargs)


@pytest.mark.django_db
@pytest.mark.usefixtures("bypass_authentication", "conversation_service")
@pytest.mark.parametrize("query", [{}, {"project_id": ""}])
def test_main_list_hides_chats_in_projects(me, my_project, query):
    outside = Conversation.objects.create(name="Unrelated", created_by=me)
    Conversation.objects.create(name="Scope", created_by=me, project=my_project)

    response = _send("get", CONVERSATIONS_PATH, data=query)

    assert response.status_code == status.HTTP_200_OK
    assert [(chat["id"], chat["project_id"]) for chat in response.data] == [
        (str(outside.uuid), None)
    ]


@pytest.mark.django_db
@pytest.mark.usefixtures("bypass_authentication", "conversation_service")
def test_list_with_a_project_id_returns_that_projects_chats(me, my_project):
    other_project = Project.objects.create(name="Onboarding", created_by=me)
    scope = Conversation.objects.create(name="Scope", created_by=me, project=my_project)
    findings = Conversation.objects.create(
        name="Findings", created_by=me, project=my_project
    )
    Conversation.objects.create(name="Welcome", created_by=me, project=other_project)
    Conversation.objects.create(name="Unrelated", created_by=me)

    response = _send("get", CONVERSATIONS_PATH, data={"project_id": str(my_project.id)})

    assert response.status_code == status.HTTP_200_OK
    assert sorted(chat["id"] for chat in response.data) == sorted(
        [str(scope.uuid), str(findings.uuid)]
    )
    assert [chat["project_id"] for chat in response.data] == [str(my_project.id)] * 2


@pytest.mark.django_db
@pytest.mark.usefixtures("bypass_authentication", "conversation_service")
@pytest.mark.parametrize(
    "project_id", ["3f1c2a4e-9b7d-4c1e-8a2f-5d6e7f8a9b0c", "not-a-uuid"]
)
def test_list_of_an_unknown_project_is_not_found(project_id):
    response = _send("get", CONVERSATIONS_PATH, data={"project_id": project_id})

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.data == {"detail": "Project not found."}


@pytest.mark.django_db
@pytest.mark.usefixtures("bypass_authentication", "conversation_service")
def test_list_of_another_users_project_is_forbidden(someone_elses_project):
    with pytest.raises(ForbiddenProjectAccessError):
        _send(
            "get",
            CONVERSATIONS_PATH,
            data={"project_id": str(someone_elses_project.id)},
        )


@pytest.mark.django_db
@pytest.mark.usefixtures("bypass_authentication", "conversation_service")
@pytest.mark.parametrize(
    "method, body", [("get", None), ("patch", {"name": "Renamed"}), ("delete", None)]
)
def test_conversation_responses_include_its_project_id(me, my_project, method, body):
    chat = Conversation.objects.create(name="Scope", created_by=me, project=my_project)

    response = _send(method, f"{CONVERSATIONS_PATH}/{chat.uuid}", body=body)

    assert response.status_code == status.HTTP_200_OK
    assert response.data["project_id"] == str(my_project.id)


@pytest.mark.django_db
@pytest.mark.usefixtures("bypass_authentication", "conversation_service")
def test_starting_a_chat_in_a_project_puts_it_there(me, my_project):
    response = _send(
        "post",
        STREAMED_CONVERSATIONS_PATH,
        body={"message": "Hello", "project_id": str(my_project.id)},
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert Conversation.objects.get(created_by=me).project_id == my_project.id


@pytest.mark.django_db
@pytest.mark.usefixtures("bypass_authentication", "conversation_service")
@pytest.mark.parametrize("project_id", ["", None])
def test_starting_a_chat_with_a_blank_project_id_puts_it_in_the_main_list(
    me, project_id
):
    response = _send(
        "post",
        STREAMED_CONVERSATIONS_PATH,
        body={"message": "Hello", "project_id": project_id},
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert Conversation.objects.get(created_by=me).project_id is None


@pytest.mark.django_db
@pytest.mark.usefixtures("bypass_authentication", "conversation_service")
@pytest.mark.parametrize("project_id", UNKNOWN_PROJECT_IDS)
def test_starting_a_chat_in_an_unknown_project_is_not_found(me, project_id):
    response = _send(
        "post",
        STREAMED_CONVERSATIONS_PATH,
        body={"message": "Hello", "project_id": project_id},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.data == {"detail": "Project not found."}
    assert not Conversation.objects.filter(created_by=me).exists()


@pytest.mark.django_db
@pytest.mark.usefixtures("bypass_authentication", "conversation_service")
def test_starting_a_chat_in_another_users_project_is_forbidden(
    me, someone_elses_project
):
    with pytest.raises(ForbiddenProjectAccessError):
        _send(
            "post",
            STREAMED_CONVERSATIONS_PATH,
            body={"message": "Hello", "project_id": str(someone_elses_project.id)},
        )

    assert not Conversation.objects.filter(created_by=me).exists()


@pytest.mark.django_db
@pytest.mark.usefixtures("bypass_authentication", "conversation_service")
@pytest.mark.parametrize("starts_in_another_project", [False, True])
def test_moving_a_chat_into_a_project(me, my_project, starts_in_another_project):
    start = (
        Project.objects.create(name="Onboarding", created_by=me)
        if starts_in_another_project
        else None
    )
    chat = Conversation.objects.create(name="Scope", created_by=me, project=start)

    response = _send(
        "patch",
        f"{CONVERSATIONS_PATH}/{chat.uuid}",
        body={"project_id": str(my_project.id)},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.data["project_id"] == str(my_project.id)
    chat.refresh_from_db()
    assert chat.project_id == my_project.id


@pytest.mark.django_db
@pytest.mark.usefixtures("bypass_authentication", "conversation_service")
def test_moving_a_chat_keeps_its_last_update_time(me, my_project):
    chat = Conversation.objects.create(name="Scope", created_by=me)
    last_update = datetime(2026, 1, 15, 9, 30, tzinfo=timezone.utc)
    Conversation.objects.filter(pk=chat.pk).update(updated_at=last_update)

    _send(
        "patch",
        f"{CONVERSATIONS_PATH}/{chat.uuid}",
        body={"project_id": str(my_project.id)},
    )

    chat.refresh_from_db()
    assert chat.project_id == my_project.id
    assert chat.updated_at == last_update


@pytest.mark.django_db
@pytest.mark.usefixtures("bypass_authentication", "conversation_service")
@pytest.mark.parametrize("project_id", UNKNOWN_PROJECT_IDS)
def test_moving_a_chat_into_an_unknown_project_is_not_found(me, project_id):
    chat = Conversation.objects.create(name="Scope", created_by=me)

    response = _send(
        "patch", f"{CONVERSATIONS_PATH}/{chat.uuid}", body={"project_id": project_id}
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.data == {"detail": "Project not found."}
    chat.refresh_from_db()
    assert chat.project_id is None


@pytest.mark.django_db
@pytest.mark.usefixtures("bypass_authentication", "conversation_service")
def test_moving_a_chat_into_another_users_project_is_forbidden(
    me, someone_elses_project
):
    chat = Conversation.objects.create(name="Scope", created_by=me)

    with pytest.raises(ForbiddenProjectAccessError):
        _send(
            "patch",
            f"{CONVERSATIONS_PATH}/{chat.uuid}",
            body={"project_id": str(someone_elses_project.id)},
        )

    chat.refresh_from_db()
    assert chat.project_id is None


@pytest.mark.django_db
@pytest.mark.usefixtures("bypass_authentication", "conversation_service")
def test_moving_another_users_chat_is_forbidden(my_project):
    someone_else = User.objects.create(username="someone.else@test.com")
    chat = Conversation.objects.create(name="Not mine", created_by=someone_else)

    with pytest.raises(ForbiddenConversationAccessError):
        _send(
            "patch",
            f"{CONVERSATIONS_PATH}/{chat.uuid}",
            body={"project_id": str(my_project.id)},
        )

    chat.refresh_from_db()
    assert chat.project_id is None


@pytest.mark.django_db
@pytest.mark.usefixtures("bypass_authentication", "conversation_service")
@pytest.mark.parametrize("project_id", [None, ""])
def test_moving_a_chat_out_of_projects_is_rejected(me, my_project, project_id):
    chat = Conversation.objects.create(name="Scope", created_by=me, project=my_project)

    response = _send(
        "patch", f"{CONVERSATIONS_PATH}/{chat.uuid}", body={"project_id": project_id}
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    chat.refresh_from_db()
    assert chat.project_id == my_project.id
