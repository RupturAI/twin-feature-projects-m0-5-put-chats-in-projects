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
from chatbot.web_conversation_service import WebConversationService

USERNAME = "test.user@test.com"
CONVERSATIONS_PATH = "/chatbot/conversations"


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
    with patch("chatbot.views.chatbot_views.Injector") as injector:
        injector.return_value.get.return_value.create_service.return_value = service
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
