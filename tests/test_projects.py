"""Tests for project creation and retrieval."""

import pytest

from memory_engine.models.domain import ProjectCreate
from memory_engine.services.project_service import ProjectAlreadyExistsError, ProjectService


def test_create_project(project_service: ProjectService):
    project = project_service.create(ProjectCreate(name="my-service", description="A service"))
    assert project.name == "my-service"
    assert project.description == "A service"
    assert project.id is not None
    assert project.created_at is not None


def test_list_projects(project_service: ProjectService):
    project_service.create(ProjectCreate(name="proj-a"))
    project_service.create(ProjectCreate(name="proj-b"))
    projects = project_service.list_all()
    assert len(projects) == 2
    names = {p.name for p in projects}
    assert names == {"proj-a", "proj-b"}


def test_duplicate_project_raises(project_service: ProjectService):
    project_service.create(ProjectCreate(name="unique"))
    with pytest.raises(ProjectAlreadyExistsError):
        project_service.create(ProjectCreate(name="unique"))


def test_get_project(project_service: ProjectService, demo_project):
    fetched = project_service.get(str(demo_project.id))
    assert fetched.id == demo_project.id
    assert fetched.name == demo_project.name


def test_api_create_and_list_projects(api_client):
    api_client.post("/projects", json={"name": "listed-project"})
    response = api_client.get("/projects")
    assert response.status_code == 200
    data = response.json()
    assert any(p["name"] == "listed-project" for p in data)


def test_api_create_project(api_client):
    response = api_client.post("/projects", json={"name": "api-created"})
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "api-created"


def test_api_duplicate_project_returns_409(api_client):
    api_client.post("/projects", json={"name": "dupe-project"})
    response = api_client.post("/projects", json={"name": "dupe-project"})
    assert response.status_code == 409
