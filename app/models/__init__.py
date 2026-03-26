# app/models/__init__.py

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


# Import order matters — Farmer must come before Field and AdvisoryRequest
# because they hold the ForeignKey references.
from .farmer import Farmer          # noqa: E402, F401
from .field import Field            # noqa: E402, F401
from .advisory import AdvisoryRequest  # noqa: E402, F401
from .detection import Detection    # noqa: E402, F401
from .user import User              # noqa: E402, F401