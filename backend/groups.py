"""User groups: create them, manage who is in them, and find users to add.

A group exists to be a sharing target (see TemplateShare in models.py and the
/sharing routes in main.py). It grants nothing on its own — membership only
matters through what has been shared with the group.

Rules:

* Any authenticated user can create a group, and becomes its first admin.
* Only members can see a group at all. Everyone else gets 404, the same "a
  response never confirms an id exists" rule the template routes follow.
* Admins rename, delete, add/remove members and change roles. Any member can
  remove THEMSELVES (leave).
* A group always keeps at least one admin, so it can never become unmanageable.
  The last admin can delete the group, but can't leave or demote themselves.
* Members are added by username, and only users who have signed in at least
  once (i.e. have an app_user row) can be added. Pre-creating rows for typed-in
  usernames would let a typo silently create a phantom user who then "has
  access".
"""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

import auth
from db import get_db
from models import AppUser, TemplateShare, UserGroup, UserGroupMember

router = APIRouter(tags=["groups"])

Role = Literal["admin", "member"]


def get_current_user(request: Request, db: Session = Depends(get_db)) -> AppUser:
    return auth.require_current_user(request, db)


# --- Lookups ------------------------------------------------------------------


def member_group_ids(db: Session, user: AppUser):
    """Subquery of the group ids `user` belongs to. Used by main.visible_templates
    to resolve group shares."""
    return db.query(UserGroupMember.group_id).filter(
        UserGroupMember.user_id == user.user_id
    ).subquery()


def _membership(db: Session, group_id: int, user_id: int) -> UserGroupMember | None:
    return db.query(UserGroupMember).filter(
        UserGroupMember.group_id == group_id,
        UserGroupMember.user_id == user_id,
    ).first()


def group_or_404(db: Session, user: AppUser, group_id: int) -> tuple[UserGroup, UserGroupMember]:
    """A group `user` is a member of, with their membership row, or 404."""
    membership = _membership(db, group_id, user.user_id)
    if not membership:
        raise HTTPException(status_code=404, detail="Group not found")
    return membership.group, membership


def admin_group_or_403(db: Session, user: AppUser, group_id: int, action: str) -> UserGroup:
    """A group `user` administers. 404 for non-members, 403 for plain members —
    they can already see the group, so saying why is no disclosure."""
    group, membership = group_or_404(db, user, group_id)
    if membership.role != "admin":
        raise HTTPException(status_code=403, detail=f"Only a group admin can {action}.")
    return group


def _admin_count(db: Session, group_id: int) -> int:
    return db.query(func.count()).select_from(UserGroupMember).filter(
        UserGroupMember.group_id == group_id,
        UserGroupMember.role == "admin",
    ).scalar()


def user_brief(u: AppUser) -> dict:
    return {"user_id": u.user_id, "username": u.username, "email": u.email}


def _group_summary(db: Session, group: UserGroup, my_role: str) -> dict:
    member_count = db.query(func.count()).select_from(UserGroupMember).filter(
        UserGroupMember.group_id == group.group_id
    ).scalar()
    return {
        "group_id": group.group_id,
        "name": group.name,
        "description": group.description or "",
        "my_role": my_role,
        "member_count": member_count,
        "created_at": group.created_at,
    }


def _group_detail(db: Session, group: UserGroup, my_role: str) -> dict:
    rows = db.query(UserGroupMember, AppUser).join(
        AppUser, AppUser.user_id == UserGroupMember.user_id
    ).filter(UserGroupMember.group_id == group.group_id).order_by(AppUser.username).all()
    return {
        **_group_summary(db, group, my_role),
        "members": [{**user_brief(u), "role": m.role, "added_at": m.added_at} for m, u in rows],
    }


# --- Users --------------------------------------------------------------------


@router.get("/api/users/search")
def search_users(q: str = "", limit: int = 10, db: Session = Depends(get_db),
                 user: AppUser = Depends(get_current_user)):
    """Users whose username or email contains `q`, for the member and share
    pickers. Requires at least 2 characters so it can't be used to page through
    the whole user table."""
    q = q.strip()
    if len(q) < 2:
        return []
    pattern = f"%{q}%"
    rows = db.query(AppUser).filter(
        or_(AppUser.username.ilike(pattern), AppUser.email.ilike(pattern))
    ).order_by(AppUser.username).limit(max(1, min(limit, 25))).all()
    return [user_brief(u) for u in rows]


# --- Groups -------------------------------------------------------------------


class GroupCreate(BaseModel):
    name: str
    description: str = ""


class GroupUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class MemberAdd(BaseModel):
    username: str
    role: Role = "member"


class MemberUpdate(BaseModel):
    role: Role


def _clean_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Group name can't be empty.")
    if len(name) > 100:
        raise HTTPException(status_code=400, detail="Group name is too long (100 characters max).")
    return name


@router.get("/api/groups")
def list_groups(db: Session = Depends(get_db), user: AppUser = Depends(get_current_user)):
    """The groups the caller belongs to."""
    rows = db.query(UserGroup, UserGroupMember.role).join(
        UserGroupMember, UserGroupMember.group_id == UserGroup.group_id
    ).filter(UserGroupMember.user_id == user.user_id).order_by(UserGroup.name).all()
    return [_group_summary(db, g, role) for g, role in rows]


@router.post("/api/groups")
def create_group(body: GroupCreate, db: Session = Depends(get_db),
                 user: AppUser = Depends(get_current_user)):
    group = UserGroup(name=_clean_name(body.name), description=body.description.strip(),
                      created_by_id=user.user_id)
    db.add(group)
    db.flush()
    db.add(UserGroupMember(group_id=group.group_id, user_id=user.user_id, role="admin"))
    db.commit()
    db.refresh(group)
    return _group_detail(db, group, "admin")


@router.get("/api/groups/{group_id}")
def get_group(group_id: int, db: Session = Depends(get_db),
              user: AppUser = Depends(get_current_user)):
    group, membership = group_or_404(db, user, group_id)
    return _group_detail(db, group, membership.role)


@router.patch("/api/groups/{group_id}")
def update_group(group_id: int, body: GroupUpdate, db: Session = Depends(get_db),
                 user: AppUser = Depends(get_current_user)):
    group = admin_group_or_403(db, user, group_id, "rename or describe this group")
    if body.name is not None:
        group.name = _clean_name(body.name)
    if body.description is not None:
        group.description = body.description.strip()
    db.commit()
    return _group_detail(db, group, "admin")


def _delete_group(group_id: int, db: Session, user: AppUser) -> dict:
    group = admin_group_or_403(db, user, group_id, "delete this group")
    # Explicit rather than relying on the FK's ON DELETE CASCADE: tables created
    # before the constraint existed won't have it, and a dangling group share
    # would be harmless but confusing in the sharing dialog.
    db.query(TemplateShare).filter(TemplateShare.group_id == group_id).delete(synchronize_session=False)
    db.delete(group)
    db.commit()
    return {"message": f"Group '{group.name}' deleted.", "group_id": group_id}


@router.delete("/api/groups/{group_id}")
def delete_group(group_id: int, db: Session = Depends(get_db),
                 user: AppUser = Depends(get_current_user)):
    """Delete a group. Workflows shared with it stop being visible to its members
    (unless shared with them some other way). Admin only."""
    return _delete_group(group_id, db, user)


# POST alias, matching /api/secrets/{key}/delete and /api/triggers/{id}/delete,
# for proxies that drop DELETE.
@router.post("/api/groups/{group_id}/delete")
def delete_group_post(group_id: int, db: Session = Depends(get_db),
                      user: AppUser = Depends(get_current_user)):
    return _delete_group(group_id, db, user)


# --- Members ------------------------------------------------------------------


@router.post("/api/groups/{group_id}/members")
def add_member(group_id: int, body: MemberAdd, db: Session = Depends(get_db),
               user: AppUser = Depends(get_current_user)):
    group = admin_group_or_403(db, user, group_id, "add members")
    username = body.username.strip()
    target = db.query(AppUser).filter(AppUser.username == username).first()
    if not target:
        raise HTTPException(
            status_code=404,
            detail=f"No user '{username}'. People can be added once they have signed in at least once.",
        )
    existing = _membership(db, group_id, target.user_id)
    if existing:
        raise HTTPException(status_code=409, detail=f"'{username}' is already in this group.")
    db.add(UserGroupMember(group_id=group_id, user_id=target.user_id, role=body.role))
    db.commit()
    return _group_detail(db, group, "admin")


@router.patch("/api/groups/{group_id}/members/{user_id}")
def update_member(group_id: int, user_id: int, body: MemberUpdate, db: Session = Depends(get_db),
                  user: AppUser = Depends(get_current_user)):
    group = admin_group_or_403(db, user, group_id, "change member roles")
    membership = _membership(db, group_id, user_id)
    if not membership:
        raise HTTPException(status_code=404, detail="Not a member of this group.")
    if membership.role == "admin" and body.role != "admin" and _admin_count(db, group_id) <= 1:
        raise HTTPException(
            status_code=400,
            detail="A group needs at least one admin. Make someone else an admin first.",
        )
    membership.role = body.role
    db.commit()
    my_role = _membership(db, group_id, user.user_id).role
    return _group_detail(db, group, my_role)


def _remove_member(group_id: int, user_id: int, db: Session, user: AppUser) -> dict:
    group, my_membership = group_or_404(db, user, group_id)
    leaving = user_id == user.user_id
    if not leaving and my_membership.role != "admin":
        raise HTTPException(status_code=403, detail="Only a group admin can remove other members.")
    membership = _membership(db, group_id, user_id)
    if not membership:
        raise HTTPException(status_code=404, detail="Not a member of this group.")
    if membership.role == "admin" and _admin_count(db, group_id) <= 1:
        raise HTTPException(
            status_code=400,
            detail="A group needs at least one admin. Make someone else an admin first, "
                   "or delete the group.",
        )
    db.delete(membership)
    db.commit()
    if leaving:
        return {"message": f"You left '{group.name}'.", "group_id": group_id, "left": True}
    return _group_detail(db, group, my_membership.role)


@router.delete("/api/groups/{group_id}/members/{user_id}")
def remove_member(group_id: int, user_id: int, db: Session = Depends(get_db),
                  user: AppUser = Depends(get_current_user)):
    """Remove a member (admin), or leave the group (pass your own user_id)."""
    return _remove_member(group_id, user_id, db, user)


@router.post("/api/groups/{group_id}/members/{user_id}/delete")
def remove_member_post(group_id: int, user_id: int, db: Session = Depends(get_db),
                       user: AppUser = Depends(get_current_user)):
    return _remove_member(group_id, user_id, db, user)
