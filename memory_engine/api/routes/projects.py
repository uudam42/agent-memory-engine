"""Project endpoints."""

from fastapi import APIRouter, Depends, HTTPException, status

from memory_engine.api.deps import project_service, tree_service
from memory_engine.models.domain import Project, ProjectCreate
from memory_engine.services.project_service import ProjectAlreadyExistsError, ProjectService
from memory_engine.services.tree_service import TreeService

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", response_model=Project, status_code=status.HTTP_201_CREATED)
def create_project(
    payload: ProjectCreate,
    svc: ProjectService = Depends(project_service),
) -> Project:
    try:
        return svc.create(payload)
    except ProjectAlreadyExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("", response_model=list[Project])
def list_projects(svc: ProjectService = Depends(project_service)) -> list[Project]:
    return svc.list_all()


@router.get("/{project_id}/tree")
def project_tree(
    project_id: str,
    svc: TreeService = Depends(tree_service),
) -> dict:
    text = svc.render_text(project_id)
    tree = svc.build_tree(project_id)

    def _serialise(nodes):  # type: ignore[no-untyped-def]
        out = []
        for tn in nodes:
            out.append(
                {
                    "id": str(tn.node.id),
                    "title": tn.node.title,
                    "kind": tn.node.kind,
                    "depth": tn.node.depth,
                    "children": _serialise(tn.children),
                }
            )
        return out

    return {"tree": _serialise(tree), "text": text}
