from drf_yasg.utils import swagger_auto_schema
from rest_framework import status
from rest_framework.parsers import MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from chatbot.api_parameters import request_id_parameter
from chatbot.authentication_mixin import AuthenticationMixin
from chatbot.openapi_constants import AUTHORISATION_ERROR_RESPONSES
from chatbot.project_dto import ProjectDto
from chatbot.project_repository import ProjectNotFoundError
from chatbot.project_service import ProjectService
from chatbot.serializers import (
    AttachmentUploadRequestSerializer,
    ProjectCreateRequestSerializer,
    ProjectDetailResponseSerializer,
    ProjectFileResponseSerializer,
    ProjectPatchRequestSerializer,
    ProjectResponseSerializer,
)

PROJECT_FORBIDDEN_RESPONSE = {403: "Another user's project"}
PROJECT_NOT_FOUND_RESPONSE = {404: "Project not found"}


def _project_not_found():
    return Response({"detail": "Project not found."}, status=status.HTTP_404_NOT_FOUND)


def _project_data(project: ProjectDto) -> dict:
    return {
        "id": str(project.id),
        "name": project.name,
        "description": project.description,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }


def _project_detail_data(project: ProjectDto, conversation_count: int) -> dict:
    return {
        **_project_data(project),
        "conversation_count": conversation_count,
        "files": [],
    }


class ProjectList(AuthenticationMixin, APIView):
    @swagger_auto_schema(
        manual_parameters=[request_id_parameter],
        responses={
            **AUTHORISATION_ERROR_RESPONSES,
            200: ProjectResponseSerializer(many=True),
        },
        operation_description="List my projects, most recently updated first.",
    )
    def get(self, request):
        projects = ProjectService(request.claims["username"]).list_projects()
        serializer = ProjectResponseSerializer(
            [_project_data(project) for project in projects], many=True
        )
        return Response(serializer.data)

    @swagger_auto_schema(
        manual_parameters=[request_id_parameter],
        request_body=ProjectCreateRequestSerializer,
        responses={
            **AUTHORISATION_ERROR_RESPONSES,
            201: ProjectDetailResponseSerializer,
            400: "Bad Request",
        },
        operation_description="Create a project.",
    )
    def post(self, request):
        request_serializer = ProjectCreateRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)

        project = ProjectService(request.claims["username"]).create_project(
            **request_serializer.validated_data
        )

        # A new project has no chats and no files yet.
        response_serializer = ProjectDetailResponseSerializer(
            {**_project_data(project), "conversation_count": 0, "files": []}
        )
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)


class ProjectDetail(AuthenticationMixin, APIView):
    @swagger_auto_schema(
        manual_parameters=[request_id_parameter],
        responses={
            **AUTHORISATION_ERROR_RESPONSES,
            200: ProjectDetailResponseSerializer,
            **PROJECT_FORBIDDEN_RESPONSE,
            **PROJECT_NOT_FOUND_RESPONSE,
        },
        operation_description="Get a project's details: name, description, files and number of chats.",
    )
    def get(self, request: Request, project_id: str) -> Response:
        service = ProjectService(request.claims["username"])
        try:
            project = service.get_project(project_id)
        except ProjectNotFoundError:
            return _project_not_found()

        response_serializer = ProjectDetailResponseSerializer(
            _project_detail_data(project, service.count_conversations(project.id))
        )
        return Response(response_serializer.data)

    @swagger_auto_schema(
        manual_parameters=[request_id_parameter],
        request_body=ProjectPatchRequestSerializer,
        responses={
            **AUTHORISATION_ERROR_RESPONSES,
            200: ProjectDetailResponseSerializer,
            400: "Bad Request",
            **PROJECT_FORBIDDEN_RESPONSE,
            **PROJECT_NOT_FOUND_RESPONSE,
        },
        operation_description="Edit a project's name or description (send at least one).",
    )
    def patch(self, request: Request, project_id: str) -> Response:
        request_serializer = ProjectPatchRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)

        service = ProjectService(request.claims["username"])
        try:
            project = service.update_project(
                project_id, **request_serializer.validated_data
            )
        except ProjectNotFoundError:
            return _project_not_found()

        response_serializer = ProjectDetailResponseSerializer(
            _project_detail_data(project, service.count_conversations(project.id))
        )
        return Response(response_serializer.data)

    @swagger_auto_schema(
        manual_parameters=[request_id_parameter],
        responses={
            **AUTHORISATION_ERROR_RESPONSES,
            204: "Project deleted with its files and chats",
            **PROJECT_FORBIDDEN_RESPONSE,
            **PROJECT_NOT_FOUND_RESPONSE,
        },
        operation_description="Delete a project with its files and chats.",
    )
    def delete(self, request: Request, project_id: str) -> Response:
        try:
            ProjectService(request.claims["username"]).delete_project(project_id)
        except ProjectNotFoundError:
            return _project_not_found()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ProjectFileUpload(AuthenticationMixin, APIView):
    parser_classes = [MultiPartParser]

    @swagger_auto_schema(
        manual_parameters=[request_id_parameter],
        request_body=AttachmentUploadRequestSerializer,
        responses={
            **AUTHORISATION_ERROR_RESPONSES,
            201: ProjectFileResponseSerializer,
            400: "Bad Request",
            **PROJECT_FORBIDDEN_RESPONSE,
            **PROJECT_NOT_FOUND_RESPONSE,
            501: "Uploading project files is not available yet",
        },
        operation_description="Upload a file (text, PDF or Word) to a project. Not implemented yet: 501.",
        consumes=["multipart/form-data"],
    )
    def post(self, request: Request, project_id: str) -> Response:
        try:
            ProjectService(request.claims["username"]).get_project(project_id)
        except ProjectNotFoundError:
            return _project_not_found()
        return Response(
            {"detail": "Uploading project files is not available yet."},
            status=status.HTTP_501_NOT_IMPLEMENTED,
        )


class ProjectFileDetail(AuthenticationMixin, APIView):
    @swagger_auto_schema(
        manual_parameters=[request_id_parameter],
        responses={
            **AUTHORISATION_ERROR_RESPONSES,
            204: "File removed from the project",
            **PROJECT_FORBIDDEN_RESPONSE,
            404: "Project or file not found",
        },
        operation_description="Remove a file from a project. Projects have no files yet: 404.",
    )
    def delete(self, request: Request, project_id: str, file_id: str) -> Response:
        try:
            ProjectService(request.claims["username"]).get_project(project_id)
        except ProjectNotFoundError:
            return _project_not_found()
        return Response({"detail": "File not found."}, status=status.HTTP_404_NOT_FOUND)
