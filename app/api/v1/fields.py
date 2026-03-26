# app/api/v1/fields.py

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.dependencies import get_db, require_extension_worker
from app.core.rate_limit import farmer_registration_limit
from app.models.field import Field
from app.schemas.field import FieldCreate, FieldResponse

router = APIRouter()


@router.post(
    "/",
    response_model=FieldResponse,
    status_code=201,
    dependencies=[Depends(farmer_registration_limit)],
)
async def create_field(
    payload: FieldCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Public endpoint — called by the farmer portal immediately after farmer
    registration. No JWT required, but rate-limited (shares the registration
    limit so the portal's 2-step register flow counts as one combined action).

    Validates that the farmer_phone actually exists before creating the field,
    so random phone numbers can't be used to create orphaned field records.
    """
    from app.models.farmer import Farmer
    farmer = await db.scalar(
        select(Farmer).where(
            Farmer.phone_number == payload.farmer_phone,
            Farmer.is_active == True,
        )
    )
    if not farmer:
        raise HTTPException(
            status_code=404,
            detail="Farmer not found. Register the farmer first.",
        )

    field = Field(**payload.model_dump())
    db.add(field)
    await db.commit()
    await db.refresh(field)
    return field


@router.get(
    "/farmer/{farmer_phone}",
    response_model=list[FieldResponse],
    dependencies=[Depends(require_extension_worker)],
)
async def get_fields_for_farmer(
    farmer_phone: str,
    db: AsyncSession = Depends(get_db),
):
    """Extension workers and admins only."""
    result = await db.execute(
        select(Field).where(
            Field.farmer_phone == farmer_phone,
            Field.is_active == True,
        )
    )
    return result.scalars().all()


@router.get(
    "/{field_id}",
    response_model=FieldResponse,
    dependencies=[Depends(require_extension_worker)],
)
async def get_field(field_id: str, db: AsyncSession = Depends(get_db)):
    """Extension workers and admins only."""
    import uuid
    result = await db.execute(
        select(Field).where(Field.id == uuid.UUID(field_id))
    )
    field = result.scalar_one_or_none()
    if not field:
        raise HTTPException(status_code=404, detail="Field not found")
    return field


@router.delete(
    "/{field_id}",
    status_code=204,
    dependencies=[Depends(require_extension_worker)],
)
async def delete_field(field_id: str, db: AsyncSession = Depends(get_db)):
    """Extension workers and admins only."""
    import uuid
    result = await db.execute(
        select(Field).where(Field.id == uuid.UUID(field_id))
    )
    field = result.scalar_one_or_none()
    if not field:
        raise HTTPException(status_code=404, detail="Field not found")
    field.is_active = False
    await db.commit()