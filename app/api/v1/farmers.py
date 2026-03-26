# app/api/v1/farmers.py

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from app.core.dependencies import get_db, require_extension_worker, get_current_user
from app.core.rate_limit import farmer_registration_limit
from app.models.farmer import Farmer
from app.models.field import Field
from app.models.advisory import AdvisoryRequest
from app.schemas.farmer import FarmerCreate, FarmerResponse

router = APIRouter()


@router.post(
    "/",
    response_model=FarmerResponse,
    status_code=201,
    dependencies=[Depends(farmer_registration_limit)],
)
async def create_farmer(
    payload: FarmerCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Public endpoint — used by the farmer portal (no login required).
    Rate-limited to 5 registrations per IP per hour to prevent spam.
    """
    result = await db.execute(
        select(Farmer).where(Farmer.phone_number == payload.phone_number)
    )
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail="Farmer with this phone number already exists",
        )
    farmer = Farmer(**payload.model_dump())
    db.add(farmer)
    await db.commit()
    await db.refresh(farmer)
    return farmer


@router.get(
    "/",
    response_model=list[FarmerResponse],
    dependencies=[Depends(require_extension_worker)],
)
async def list_farmers(db: AsyncSession = Depends(get_db)):
    """Extension workers and admins only."""
    result = await db.execute(
        select(Farmer).where(Farmer.is_active == True)
    )
    return result.scalars().all()


@router.get(
    "/check",
    summary="Check if a phone number is already registered",
)
async def check_farmer_exists(
    phone: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _=Depends(farmer_registration_limit),
):
    """
    Used by the farmer portal registration flow to validate phone before submitting.

    Returns {"exists": true/false} — deliberately no farmer data in the response
    to prevent enumeration of farmer details.

    Rate-limited (shares the registration limit) so it can't be used to bulk-probe
    phone numbers.
    """
    result = await db.execute(
        select(Farmer.id).where(
            Farmer.phone_number == phone,
            Farmer.is_active == True,
        )
    )
    exists = result.scalar_one_or_none() is not None
    return {"exists": exists}


@router.get(
    "/{farmer_id}",
    response_model=FarmerResponse,
    dependencies=[Depends(require_extension_worker)],
)
async def get_farmer(farmer_id: str, db: AsyncSession = Depends(get_db)):
    """
    Extension workers and admins only.

    Was previously unauthenticated — anyone could query any phone number
    and confirm whether that person is a registered farmer (enumeration attack).
    """
    result = await db.execute(
        select(Farmer).where(Farmer.phone_number == farmer_id)
    )
    farmer = result.scalar_one_or_none()
    if not farmer:
        raise HTTPException(status_code=404, detail="Farmer not found")
    return farmer


@router.delete(
    "/{farmer_id}",
    status_code=204,
    dependencies=[Depends(require_extension_worker)],
)
async def delete_farmer(farmer_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Farmer).where(Farmer.phone_number == farmer_id)
    )
    farmer = result.scalar_one_or_none()
    if not farmer:
        raise HTTPException(status_code=404, detail="Farmer not found")

    # Hard delete — cascades to fields and advisories via FK constraint
    await db.execute(delete(Field).where(Field.farmer_phone == farmer_id))
    await db.execute(
        delete(AdvisoryRequest).where(AdvisoryRequest.farmer_phone == farmer_id)
    )
    await db.execute(delete(Farmer).where(Farmer.phone_number == farmer_id))
    await db.commit()