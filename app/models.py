from sqlalchemy import Column, String, Integer, Boolean, DateTime, Numeric, JSON, Text, ForeignKey, Index, BigInteger
from sqlalchemy.dialects.postgresql import UUID, INET
from sqlalchemy.sql import func
from app.database import Base
import uuid


class Pharmacy(Base):
    __tablename__ = "pharmacies"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)
    hwid = Column(String(64), unique=True, nullable=False)
    owner_name = Column(String(100))
    phone = Column(String(20))
    email = Column(String(100))
    address = Column(Text)
    city = Column(String(50))
    province = Column(String(50))

    subscription_type = Column(String(20), default="trial")
    subscription_expiry = Column(DateTime)
    is_active = Column(Boolean, default=True)

    last_sync_at = Column(DateTime)
    sync_version = Column(BigInteger, default=0)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pharmacy_id = Column(UUID(as_uuid=True), ForeignKey("pharmacies.id"), nullable=False)
    role_id = Column(UUID(as_uuid=True), ForeignKey("roles.id"), nullable=False)

    fullname = Column(String(100), nullable=False)
    username = Column(String(50), unique=True, nullable=False)
    password_hash = Column(Text, nullable=False)
    email = Column(String(100), unique=True)
    phone = Column(String(20))

    is_active = Column(Boolean, default=True)
    is_verified = Column(Boolean, default=False)

    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    is_deleted = Column(Boolean, default=False)
    sync_version = Column(Integer, default=1)
    source = Column(String(20), default="desktop")

    created_at = Column(DateTime, server_default=func.now())
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))


class Role(Base):
    __tablename__ = "roles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(50), unique=True, nullable=False)
    description = Column(Text)
    permissions = Column(JSON, default={})
    created_at = Column(DateTime, server_default=func.now())


# Customer model (reference for other models - implement similarly)
class Customer(Base):
    __tablename__ = "customers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pharmacy_id = Column(UUID(as_uuid=True), ForeignKey("pharmacies.id"), nullable=False)

    full_name = Column(String(100), nullable=False)
    phone = Column(String(20))
    address = Column(Text)

    balance = Column(Numeric(10, 2), default=0)
    credit_limit = Column(Numeric(10, 2), default=0)
    total_purchases = Column(Numeric(10, 2), default=0)
    total_payments = Column(Numeric(10, 2), default=0)
    last_payment_date = Column(DateTime)

    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    is_deleted = Column(Boolean, default=False)
    sync_version = Column(Integer, default=1)
    source = Column(String(20), default="desktop")

    created_at = Column(DateTime, server_default=func.now())
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))


class Categories(Base):
    __tablename__ = "categories"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pharmacy_id = Column(UUID(as_uuid=True), ForeignKey("pharmacies.id"), nullable=False)
    name = Column(String(50), nullable=False)
    description = Column(Text)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    is_deleted = Column(Boolean, default=False)
    sync_version = Column(Integer, default=1)
    source = Column(String(20), default="desktop")
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)


class BatchUnits(Base):
    __tablename__ = "batch_units"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pharmacy_id = Column(UUID(as_uuid=True), ForeignKey("pharmacies.id"), nullable=False)
    batch_id = Column(UUID(as_uuid=True), ForeignKey("batches.id"), unique=True, nullable=False)
    unit_type_id = Column(UUID(as_uuid=True), ForeignKey("unit_types.id"), nullable=False)
    pack_size = Column(Integer, nullable=True)
    subunit_size = Column(Integer, nullable=True)
    smallest_unit_factor = Column(Integer, nullable=False, default=1)
    purchase_price_per_unit = Column(Numeric(10, 2), nullable=False)
    selling_price_per_unit = Column(Numeric(10, 2), nullable=False)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    is_deleted = Column(Boolean, default=False)
    sync_version = Column(Integer, default=1)
    source = Column(String(20), default="desktop")


class MedicinePackagingTemplate(Base):
    __tablename__ = "medicine_packaging_templates"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pharmacy_id = Column(UUID(as_uuid=True), ForeignKey("pharmacies.id"), nullable=False)
    medicine_id = Column(UUID(as_uuid=True), ForeignKey("medicines.id"), nullable=False)
    purchase_unit_id = Column(UUID(as_uuid=True), ForeignKey("unit_types.id"), nullable=False)
    pack_size = Column(Integer, nullable=True)
    subunit_size = Column(Integer, nullable=True)
    smallest_unit_factor = Column(Integer, nullable=False, default=1)
    is_default = Column(Boolean, default=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    is_deleted = Column(Boolean, default=False)
    sync_version = Column(Integer, default=1)
    source = Column(String(20), default="desktop")