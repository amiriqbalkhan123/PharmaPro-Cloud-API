from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text
from uuid import UUID
from typing import List
from sqlalchemy import func, and_, or_
from datetime import datetime, timedelta
import uuid
from typing import Optional
from app.config import settings
from app.database import get_db
from app.schemas import *
from app.auth import authenticate_user, create_access_token
from pydantic import BaseModel


from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt

security = HTTPBearer()

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Validate JWT token and return user info"""
    token = credentials.credentials
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return {
            "sub": payload.get("sub"),
            "username": payload.get("username"),
            "pharmacy_id": payload.get("pharmacy_id"),
            "role": payload.get("role")
        }
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token"
        )








app = FastAPI(title=settings.APP_NAME, debug=settings.DEBUG)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Change to your domain later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================
# DASHBOARD - Mobile Home Screen
# ============================================
@app.get("/api/dashboard/stats")
async def get_dashboard_stats(
        pharmacy_id: str,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """
    Get dashboard statistics for mobile app
    Returns: today_sales, low_stock_count, expiring_count, etc.
    """
    today = datetime.now().date()
    today_start = datetime(today.year, today.month, today.day)

    # Today's sales
    today_sales_result = await db.execute(
        text("""
            SELECT COALESCE(SUM(net_amount), 0), COUNT(*)
            FROM invoices 
            WHERE pharmacy_id = :pharmacy_id 
                AND created_at >= :today_start
                AND is_deleted = FALSE
        """),
        {"pharmacy_id": UUID(pharmacy_id), "today_start": today_start}
    )
    today_total, today_count = today_sales_result.fetchone()

    # Low stock count (medicines with quantity_remaining < threshold)
    low_stock_result = await db.execute(
        text("""
            SELECT COUNT(DISTINCT m.id)
            FROM medicines m
            JOIN batches b ON b.medicine_id = m.id
            WHERE m.pharmacy_id = :pharmacy_id
                AND b.quantity_remaining < 10
                AND b.is_deleted = FALSE
                AND m.is_deleted = FALSE
        """),
        {"pharmacy_id": UUID(pharmacy_id)}
    )
    low_stock_count = low_stock_result.scalar() or 0

    # Expiring soon (within 60 days)
    expiring_result = await db.execute(
        text("""
            SELECT COUNT(*)
            FROM batches
            WHERE pharmacy_id = :pharmacy_id
                AND expiry_date BETWEEN CURRENT_DATE AND CURRENT_DATE + 60
                AND quantity_remaining > 0
                AND is_deleted = FALSE
        """),
        {"pharmacy_id": UUID(pharmacy_id)}
    )
    expiring_count = expiring_result.scalar() or 0

    # Total customers
    customers_result = await db.execute(
        text("SELECT COUNT(*) FROM customers WHERE pharmacy_id = :pharmacy_id AND is_deleted = FALSE"),
        {"pharmacy_id": UUID(pharmacy_id)}
    )
    total_customers = customers_result.scalar() or 0

    # Total medicines
    medicines_result = await db.execute(
        text("SELECT COUNT(*) FROM medicines WHERE pharmacy_id = :pharmacy_id AND is_deleted = FALSE"),
        {"pharmacy_id": UUID(pharmacy_id)}
    )
    total_medicines = medicines_result.scalar() or 0

    # Recent sales (last 5)
    recent_sales = await db.execute(
        text("""
            SELECT i.id, i.invoice_number, i.net_amount, i.created_at, c.full_name as customer_name
            FROM invoices i
            LEFT JOIN customers c ON c.id = i.customer_id
            WHERE i.pharmacy_id = :pharmacy_id AND i.is_deleted = FALSE
            ORDER BY i.created_at DESC
            LIMIT 5
        """),
        {"pharmacy_id": UUID(pharmacy_id)}
    )
    recent = []
    for row in recent_sales:
        recent.append({
            "id": str(row[0]),
            "invoice_number": row[1],
            "amount": float(row[2]),
            "date": row[3].isoformat() if row[3] else None,
            "customer": row[4] or "Walk-in"
        })

    return {
        "success": True,
        "data": {
            "today_sales_amount": float(today_total or 0),
            "today_sales_count": today_count or 0,
            "low_stock_count": low_stock_count,
            "expiring_soon_count": expiring_count,
            "total_customers": total_customers,
            "total_medicines": total_medicines,
            "recent_sales": recent
        }
    }


# ============================================
# PHARMACY REGISTRATION
# ============================================
class RegisterPharmacyRequest(BaseModel):
    hwid: str
    name: str = "Pharmacy"


@app.post("/api/pharmacy/register")
async def register_pharmacy(
        request: RegisterPharmacyRequest,
        db: AsyncSession = Depends(get_db)
):
    """Register a new pharmacy installation"""

    # Check if pharmacy already exists
    result = await db.execute(
        text("SELECT id FROM pharmacies WHERE hwid = :hwid"),
        {"hwid": request.hwid}
    )
    existing = result.fetchone()

    if existing:
        return {
            "success": True,
            "pharmacy_id": str(existing[0]),
            "message": "Pharmacy already registered"
        }

    # Create new pharmacy
    pharmacy_id = uuid.uuid4()
    await db.execute(
        text("""
            INSERT INTO pharmacies (id, name, hwid, subscription_type, is_active)
            VALUES (:id, :name, :hwid, 'trial', TRUE)
        """),
        {"id": pharmacy_id, "name": request.name, "hwid": request.hwid}
    )

    # Create sync_state for this pharmacy
    await db.execute(
        text("""
            INSERT INTO sync_state (pharmacy_id, last_sync_version, sync_status)
            VALUES (:pharmacy_id, 0, 'idle')
        """),
        {"pharmacy_id": pharmacy_id}
    )

    await db.commit()

    return {
        "success": True,
        "pharmacy_id": str(pharmacy_id),
        "message": "Pharmacy registered successfully"
    }


# ============================================
# MEDICINES - Search & List
# ============================================
@app.get("/api/medicines/search")
async def search_medicines(
        pharmacy_id: str,
        q: str = "",
        page: int = 1,
        limit: int = 20,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """
    Search medicines by name, brand, or barcode
    Returns stock levels with batch breakdown
    """
    offset = (page - 1) * limit

    # Build search condition
    search_condition = ""
    params = {"pharmacy_id": UUID(pharmacy_id), "limit": limit, "offset": offset}

    if q:
        search_condition = """
            AND (m.name ILIKE :search 
                OR m.brand ILIKE :search 
                OR m.barcode ILIKE :search
                OR m.generic_name ILIKE :search)
        """
        params["search"] = f"%{q}%"

    # Get medicines with stock summary
    query = text(f"""
        SELECT 
            m.id,
            m.name,
            m.brand,
            m.generic_name,
            m.barcode,
            m.strength,
            m.dosage_form,
            COALESCE(SUM(b.quantity_remaining), 0) as total_stock,
            COUNT(DISTINCT b.id) as batch_count,
            MIN(b.expiry_date) as earliest_expiry,
            EXISTS (
                SELECT 1 FROM batches b2 
                WHERE b2.medicine_id = m.id 
                    AND b2.quantity_remaining < 10 
                    AND b2.is_deleted = FALSE
            ) as is_low_stock
        FROM medicines m
        LEFT JOIN batches b ON b.medicine_id = m.id AND b.is_deleted = FALSE
        WHERE m.pharmacy_id = :pharmacy_id
            AND m.is_deleted = FALSE
            {search_condition}
        GROUP BY m.id
        ORDER BY m.name
        LIMIT :limit OFFSET :offset
    """)

    result = await db.execute(query, params)

    medicines = []
    for row in result:
        medicines.append({
            "id": str(row[0]),
            "name": row[1],
            "brand": row[2],
            "generic_name": row[3],
            "barcode": row[4],
            "strength": row[5],
            "dosage_form": row[6],
            "total_stock": row[7] or 0,
            "batch_count": row[8] or 0,
            "earliest_expiry": row[9].isoformat() if row[9] else None,
            "is_low_stock": row[10] or False
        })

    # Get total count
    count_query = text(f"""
        SELECT COUNT(*)
        FROM medicines m
        WHERE m.pharmacy_id = :pharmacy_id
            AND m.is_deleted = FALSE
            {search_condition}
    """)
    count_result = await db.execute(count_query, params)
    total = count_result.scalar() or 0

    return {
        "success": True,
        "data": medicines,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "pages": (total + limit - 1) // limit
        }
    }


# ============================================
# CREATE MEDICINE
# ============================================
class CreateMedicineRequest(BaseModel):
    name: str
    generic_name: Optional[str] = None
    brand: Optional[str] = None
    dosage_form: Optional[str] = None
    strength: Optional[str] = None
    barcode: Optional[str] = None
    category: Optional[str] = None


@app.post("/api/medicines")
async def create_medicine(
        request: CreateMedicineRequest,
        pharmacy_id: str,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """Add a new medicine to inventory with validation"""

    # Validation: Medicine name is required
    if not request.name or not request.name.strip():
        return {"success": False, "error": "Medicine name is required"}

    # Validation: Limit name length (database has VARCHAR(100))
    MAX_NAME_LENGTH = 100
    if len(request.name) > MAX_NAME_LENGTH:
        return {"success": False,
                "error": f"Medicine name too long. Maximum {MAX_NAME_LENGTH} characters, got {len(request.name)}"}

    # Validation: Limit barcode length if provided
    if request.barcode and len(request.barcode) > 50:
        return {"success": False, "error": "Barcode too long. Maximum 50 characters"}

    medicine_id = uuid.uuid4()

    await db.execute(
        text("""
            INSERT INTO medicines (id, pharmacy_id, name, generic_name, brand, 
                dosage_form, strength, barcode, category, created_by, source)
            VALUES (:id, :pharmacy_id, :name, :generic_name, :brand, 
                :dosage_form, :strength, :barcode, :category, :created_by, 'mobile')
        """),
        {
            "id": medicine_id,
            "pharmacy_id": UUID(pharmacy_id),
            "name": request.name.strip(),
            "generic_name": request.generic_name,
            "brand": request.brand,
            "dosage_form": request.dosage_form,
            "strength": request.strength,
            "barcode": request.barcode,
            "category": request.category,
            "created_by": UUID(current_user.get("sub"))
        }
    )

    await db.commit()

    return {
        "success": True,
        "data": {"id": str(medicine_id)}
    }
# ============================================
# UPDATE MEDICINE
# ============================================
class UpdateMedicineRequest(BaseModel):
    name: Optional[str] = None
    generic_name: Optional[str] = None
    brand: Optional[str] = None
    dosage_form: Optional[str] = None
    strength: Optional[str] = None
    barcode: Optional[str] = None
    category: Optional[str] = None


@app.put("/api/medicines/{medicine_id}")
async def update_medicine(
        medicine_id: str,
        request: UpdateMedicineRequest,
        pharmacy_id: str,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """Update medicine information"""

    # Build dynamic update query
    updates = []
    params = {"id": UUID(medicine_id), "pharmacy_id": UUID(pharmacy_id)}

    if request.name:
        updates.append("name = :name")
        params["name"] = request.name
    if request.generic_name:
        updates.append("generic_name = :generic_name")
        params["generic_name"] = request.generic_name
    if request.brand:
        updates.append("brand = :brand")
        params["brand"] = request.brand
    if request.dosage_form:
        updates.append("dosage_form = :dosage_form")
        params["dosage_form"] = request.dosage_form
    if request.strength:
        updates.append("strength = :strength")
        params["strength"] = request.strength
    if request.barcode:
        updates.append("barcode = :barcode")
        params["barcode"] = request.barcode
    if request.category:
        updates.append("category = :category")
        params["category"] = request.category

    if not updates:
        return {"success": False, "error": "No fields to update"}

    updates.append("updated_at = CURRENT_TIMESTAMP")
    updates.append("sync_version = sync_version + 1")  # ← ADD THIS

    query = text(f"""
        UPDATE medicines 
        SET {', '.join(updates)}
        WHERE id = :id AND pharmacy_id = :pharmacy_id AND is_deleted = FALSE
    """)

    result = await db.execute(query, params)
    await db.commit()

    if result.rowcount == 0:
        return {"success": False, "error": "Medicine not found"}

    return {"success": True, "message": "Medicine updated"}


# ============================================
# DELETE MEDICINE (Soft Delete)
# ============================================
@app.delete("/api/medicines/{medicine_id}")
async def delete_medicine(
        medicine_id: str,
        pharmacy_id: str,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """Soft delete a medicine (won't appear in lists)"""

    await db.execute(
        text("""
            UPDATE medicines 
            SET is_deleted = TRUE, updated_at = CURRENT_TIMESTAMP
            WHERE id = :id AND pharmacy_id = :pharmacy_id
        """),
        {"id": UUID(medicine_id), "pharmacy_id": UUID(pharmacy_id)}
    )

    await db.commit()

    return {"success": True, "message": "Medicine deleted"}


# ============================================
# ADD BATCH TO MEDICINE
# ============================================
class AddBatchRequest(BaseModel):
    batch_number: str
    expiry_date: str  # YYYY-MM-DD
    purchase_price: float
    selling_price: float
    quantity: int


@app.post("/api/medicines/{medicine_id}/batches")
async def add_batch(
        medicine_id: str,
        request: AddBatchRequest,
        pharmacy_id: str,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """Add a new batch (stock) to a medicine"""

    # Verify medicine exists
    medicine_check = await db.execute(
        text("SELECT id FROM medicines WHERE id = :id AND pharmacy_id = :pharmacy_id AND is_deleted = FALSE"),
        {"id": UUID(medicine_id), "pharmacy_id": UUID(pharmacy_id)}
    )
    if not medicine_check.fetchone():
        return {"success": False, "error": "Medicine not found"}

    batch_id = uuid.uuid4()

    await db.execute(
        text("""
            INSERT INTO batches (id, pharmacy_id, medicine_id, batch_number, expiry_date, 
                purchase_price, selling_price, quantity_received, quantity_remaining, created_by, source)
            VALUES (:id, :pharmacy_id, :medicine_id, :batch_number, :expiry_date, 
                :purchase_price, :selling_price, :quantity, :quantity, :created_by, 'mobile')
        """),
        {
            "id": batch_id,
            "pharmacy_id": UUID(pharmacy_id),
            "medicine_id": UUID(medicine_id),
            "batch_number": request.batch_number,
            "expiry_date": datetime.strptime(request.expiry_date, "%Y-%m-%d").date(),
            "purchase_price": request.purchase_price,
            "selling_price": request.selling_price,
            "quantity": request.quantity,
            "created_by": UUID(current_user.get("sub"))
        }
    )

    await db.commit()

    return {
        "success": True,
        "data": {"batch_id": str(batch_id)}
    }


# ============================================
# MEDICINE DETAILS - Stock Breakdown
# ============================================
@app.get("/api/medicines/{medicine_id}/stock")
async def get_medicine_stock(
        medicine_id: str,
        pharmacy_id: str,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """Get detailed stock information for a medicine
    Returns all batches with expiry dates, unit info, and correct status
    """
    # Get medicine info
    medicine_result = await db.execute(
        text("""
            SELECT id, name, brand, generic_name, barcode, strength
            FROM medicines
            WHERE id = :medicine_id AND pharmacy_id = :pharmacy_id AND is_deleted = FALSE
        """),
        {"medicine_id": UUID(medicine_id), "pharmacy_id": UUID(pharmacy_id)}
    )
    medicine = medicine_result.fetchone()

    if not medicine:
        return {"success": False, "error": "Medicine not found"}

    # Get batches with unit info from batch_units table
    batches_result = await db.execute(
        text("""
            SELECT 
                b.id,
                b.batch_number,
                b.expiry_date,
                b.selling_price,
                b.quantity_remaining,
                bu.unit_type_id,
                ut.name as unit_type_name,
                bu.pack_size,
                bu.subunit_size,
                bu.smallest_unit_factor,
                CASE 
                    WHEN b.quantity_remaining = 0 THEN 'Out of Stock'
                    WHEN b.expiry_date < CURRENT_DATE THEN 'Expired'
                    WHEN b.expiry_date <= CURRENT_DATE + INTERVAL '60 days' THEN 'Expiring Soon'
                    WHEN b.quantity_remaining < 10 THEN 'Low Stock'
                    ELSE 'In Stock'
                END as status
            FROM batches b
            LEFT JOIN batch_units bu ON bu.batch_id = b.id AND bu.is_deleted = FALSE
            LEFT JOIN unit_types ut ON ut.id = bu.unit_type_id
            WHERE b.medicine_id = :medicine_id 
                AND b.pharmacy_id = :pharmacy_id
                AND b.is_deleted = FALSE
            ORDER BY b.expiry_date ASC
        """),
        {"medicine_id": UUID(medicine_id), "pharmacy_id": UUID(pharmacy_id)}
    )

    batches = []
    total_stock = 0
    for row in batches_result:
        qty = row[4] or 0
        total_stock += qty

        # Build unit_info if available
        unit_info = None
        if row[5]:  # unit_type_id exists
            unit_info = {
                "unit_type_id": str(row[5]),
                "unit_name": row[6],
                "pack_size": row[7],
                "subunit_size": row[8],
                "smallest_unit_factor": row[9]
            }

        batches.append({
            "id": str(row[0]),
            "batch_number": row[1],
            "expiry_date": row[2].isoformat() if row[2] else None,
            "selling_price": float(row[3]),
            "quantity": qty,
            "status": row[10],
            "unit_info": unit_info  # NEW: Include unit information
        })

    return {
        "success": True,
        "data": {
            "id": str(medicine[0]),
            "name": medicine[1],
            "brand": medicine[2],
            "generic_name": medicine[3],
            "barcode": medicine[4],
            "strength": medicine[5],
            "total_stock": total_stock,
            "batches": batches
        }
    }


# ============================================
# UPDATE SALE (Void/Credit)
# ============================================
class UpdateSaleRequest(BaseModel):
    status: str  # 'Paid', 'Void', 'Credit'
    discount: Optional[float] = None

@app.put("/api/sales/{invoice_id}")
async def update_sale(
        invoice_id: str,
        request: UpdateSaleRequest,
        pharmacy_id: str,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """Update sale status (void, change discount)"""

    # Check if invoice exists
    result = await db.execute(
        text("SELECT id, status FROM invoices WHERE id = :id AND pharmacy_id = :pharmacy_id AND is_deleted = FALSE"),
        {"id": UUID(invoice_id), "pharmacy_id": UUID(pharmacy_id)}
    )
    invoice = result.fetchone()

    if not invoice:
        return {"success": False, "error": "Invoice not found"}

    # If voiding, restore stock
    if request.status == "Void" and invoice[1] != "Void":
        # Get invoice details to restore stock
        details = await db.execute(
            text("SELECT batch_id, quantity FROM invoice_details WHERE invoice_id = :invoice_id"),
            {"invoice_id": UUID(invoice_id)}
        )

        for detail in details:
            await db.execute(
                text("UPDATE batches SET quantity_remaining = quantity_remaining + :qty WHERE id = :batch_id"),
                {"qty": detail[1], "batch_id": detail[0]}
            )

    # Update invoice
    update_fields = ["status = :status", "updated_at = CURRENT_TIMESTAMP", "sync_version = sync_version + 1"]  # ← ADD sync_version
    params = {"status": request.status, "id": UUID(invoice_id)}

    if request.discount is not None:
        update_fields.append("discount = :discount")
        params["discount"] = request.discount
        # Recalculate net_amount
        await db.execute(
            text("""
                UPDATE invoices 
                SET net_amount = total_amount - :discount
                WHERE id = :id
            """),
            {"discount": request.discount, "id": UUID(invoice_id)}
        )

    await db.execute(
        text(f"UPDATE invoices SET {', '.join(update_fields)} WHERE id = :id"),
        params
    )

    await db.commit()

    return {"success": True, "message": f"Sale {request.status}"}



# ============================================
# DELETE SALE (Soft Delete)
# ============================================
@app.delete("/api/sales/{invoice_id}")
async def delete_sale(
        invoice_id: str,
        pharmacy_id: str,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """Soft delete an invoice"""

    await db.execute(
        text("""
            UPDATE invoices 
            SET is_deleted = TRUE, updated_at = CURRENT_TIMESTAMP
            WHERE id = :id AND pharmacy_id = :pharmacy_id
        """),
        {"id": UUID(invoice_id), "pharmacy_id": UUID(pharmacy_id)}
    )

    await db.commit()

    return {"success": True, "message": "Sale deleted"}

@app.get("/api/sales")
async def get_sales(
        pharmacy_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        customer_id: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        limit: int = 20,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """Get sales with filters"""

    offset = (page - 1) * limit

    conditions = ["i.pharmacy_id = :pharmacy_id", "i.is_deleted = FALSE"]
    params = {"pharmacy_id": UUID(pharmacy_id), "limit": limit, "offset": offset}

    if start_date:
        conditions.append("i.created_at >= :start_date")
        params["start_date"] = start_date
    if end_date:
        conditions.append("i.created_at <= :end_date")
        params["end_date"] = end_date
    if customer_id:
        conditions.append("i.customer_id = :customer_id")
        params["customer_id"] = UUID(customer_id)
    if status:
        conditions.append("i.status = :status")
        params["status"] = status

    where_clause = " AND ".join(conditions)

    query = text(f"""
        SELECT i.id, i.invoice_number, i.created_at, i.net_amount, i.status, 
               i.is_credit, i.balance_due, c.full_name as customer_name
        FROM invoices i
        LEFT JOIN customers c ON c.id = i.customer_id
        WHERE {where_clause}
        ORDER BY i.created_at DESC
        LIMIT :limit OFFSET :offset
    """)

    result = await db.execute(query, params)

    sales = []
    for row in result:
        sales.append({
            "id": str(row[0]),
            "invoice_number": row[1],
            "created_at": row[2].isoformat() if row[2] else None,
            "amount": float(row[3]),
            "status": row[4],
            "is_credit": row[5],
            "balance_due": float(row[6]) if row[6] else 0,
            "customer_name": row[7] or "Walk-in"
        })

    # Get total count
    count_query = text(f"SELECT COUNT(*) FROM invoices i WHERE {where_clause}")
    count_result = await db.execute(count_query, params)
    total = count_result.scalar() or 0

    return {
        "success": True,
        "data": sales,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "pages": (total + limit - 1) // limit
        }
    }
# ============================================
# DELETE CUSTOMER (Soft Delete)
# ============================================
@app.delete("/api/customers/{customer_id}")
async def delete_customer(
        customer_id: str,
        pharmacy_id: str,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """Soft delete a customer"""

    # Check if customer has balance
    result = await db.execute(
        text("SELECT balance FROM customers WHERE id = :id AND pharmacy_id = :pharmacy_id"),
        {"id": UUID(customer_id), "pharmacy_id": UUID(pharmacy_id)}
    )
    customer = result.fetchone()

    if customer and customer[0] != 0:
        return {"success": False, "error": "Cannot delete customer with outstanding balance"}

    await db.execute(
        text("""
            UPDATE customers 
            SET is_deleted = TRUE, updated_at = CURRENT_TIMESTAMP
            WHERE id = :id AND pharmacy_id = :pharmacy_id
        """),
        {"id": UUID(customer_id), "pharmacy_id": UUID(pharmacy_id)}
    )

    await db.commit()

    return {"success": True, "message": "Customer deleted"}


# ============================================
# UPDATE CUSTOMER
# ============================================
class UpdateCustomerRequest(BaseModel):
    full_name: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    credit_limit: Optional[float] = None


@app.put("/api/customers/{customer_id}")
async def update_customer(
        customer_id: str,
        request: UpdateCustomerRequest,
        pharmacy_id: str,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """Update customer information"""

    updates = []
    params = {"id": UUID(customer_id), "pharmacy_id": UUID(pharmacy_id)}

    if request.full_name:
        updates.append("full_name = :full_name")
        params["full_name"] = request.full_name
    if request.phone:
        updates.append("phone = :phone")
        params["phone"] = request.phone
    if request.address:
        updates.append("address = :address")
        params["address"] = request.address
    if request.credit_limit is not None:
        updates.append("credit_limit = :credit_limit")
        params["credit_limit"] = request.credit_limit

    if not updates:
        return {"success": False, "error": "No fields to update"}

    updates.append("updated_at = CURRENT_TIMESTAMP")
    updates.append("sync_version = sync_version + 1")

    await db.execute(
        text(f"UPDATE customers SET {', '.join(updates)} WHERE id = :id AND pharmacy_id = :pharmacy_id"),
        params
    )

    await db.commit()

    return {"success": True, "message": "Customer updated"}


# ============================================
# ENHANCED CUSTOMER SEARCH
# ============================================
@app.get("/api/customers/search")
async def search_customers(
        pharmacy_id: str,
        q: str,
        limit: int = 20,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """Quick search customers by name or phone"""

    result = await db.execute(
        text("""
            SELECT id, full_name, phone, balance
            FROM customers
            WHERE pharmacy_id = :pharmacy_id
                AND is_deleted = FALSE
                AND (full_name ILIKE :search OR phone ILIKE :search)
            ORDER BY full_name
            LIMIT :limit
        """),
        {"pharmacy_id": UUID(pharmacy_id), "search": f"%{q}%", "limit": limit}
    )

    customers = []
    for row in result:
        customers.append({
            "id": str(row[0]),
            "full_name": row[1],
            "phone": row[2],
            "balance": float(row[3]) if row[3] else 0
        })

    return {"success": True, "data": customers}


# ============================================
# UPDATE SUPPLIER
# ============================================
class UpdateSupplierRequest(BaseModel):
    name: Optional[str] = None
    contact_person: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None

@app.put("/api/suppliers/{supplier_id}")
async def update_supplier(
        supplier_id: str,
        request: UpdateSupplierRequest,
        pharmacy_id: str,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """Update supplier information"""

    updates = []
    params = {"id": UUID(supplier_id), "pharmacy_id": UUID(pharmacy_id)}

    if request.name:
        updates.append("name = :name")
        params["name"] = request.name
    if request.contact_person:
        updates.append("contact_person = :contact_person")
        params["contact_person"] = request.contact_person
    if request.phone:
        updates.append("phone = :phone")
        params["phone"] = request.phone
    if request.email:
        updates.append("email = :email")
        params["email"] = request.email
    if request.address:
        updates.append("address = :address")
        params["address"] = request.address

    if not updates:
        return {"success": False, "error": "No fields to update"}

    updates.append("updated_at = CURRENT_TIMESTAMP")
    updates.append("sync_version = sync_version + 1")  # ← ADD THIS

    await db.execute(
        text(f"UPDATE suppliers SET {', '.join(updates)} WHERE id = :id AND pharmacy_id = :pharmacy_id"),
        params
    )

    await db.commit()

    return {"success": True, "message": "Supplier updated"}


# ============================================
# DELETE SUPPLIER (Soft Delete)
# ============================================
@app.delete("/api/suppliers/{supplier_id}")
async def delete_supplier(
        supplier_id: str,
        pharmacy_id: str,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """Soft delete a supplier"""

    # Check if supplier has bills
    result = await db.execute(
        text("SELECT COUNT(*) FROM bills WHERE supplier_id = :supplier_id AND is_deleted = FALSE"),
        {"supplier_id": UUID(supplier_id)}
    )
    bill_count = result.scalar() or 0

    if bill_count > 0:
        return {"success": False, "error": f"Cannot delete supplier with {bill_count} existing bills"}

    await db.execute(
        text("""
            UPDATE suppliers 
            SET is_deleted = TRUE, updated_at = CURRENT_TIMESTAMP
            WHERE id = :id AND pharmacy_id = :pharmacy_id
        """),
        {"id": UUID(supplier_id), "pharmacy_id": UUID(pharmacy_id)}
    )

    await db.commit()

    return {"success": True, "message": "Supplier deleted"}


# ============================================
# PURCHASE BILLS - List with filters
# ============================================
@app.get("/api/bills")
async def get_bills(
        pharmacy_id: str,
        supplier_id: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        limit: int = 20,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """Get purchase bills with filters"""
    offset = (page - 1) * limit

    conditions = ["b.pharmacy_id = :pharmacy_id", "b.is_deleted = FALSE"]
    params = {"pharmacy_id": UUID(pharmacy_id), "limit": limit, "offset": offset}

    if supplier_id:
        conditions.append("b.supplier_id = :supplier_id")
        params["supplier_id"] = UUID(supplier_id)
    if start_date:
        conditions.append("b.bill_date >= :start_date")
        params["start_date"] = start_date
    if end_date:
        conditions.append("b.bill_date <= :end_date")
        params["end_date"] = end_date
    if status:
        conditions.append("b.status = :status")
        params["status"] = status

    where_clause = " AND ".join(conditions)

    query = text(f"""
        SELECT b.id, b.bill_number, b.bill_date, b.total_amount, b.discount, 
               b.net_amount, b.status, b.is_credit, b.amount_paid, b.balance_due,
               b.due_date, b.created_at,
               s.id as supplier_id, s.name as supplier_name,
               u.fullname as created_by_name
        FROM bills b
        LEFT JOIN suppliers s ON s.id = b.supplier_id
        LEFT JOIN users u ON u.id = b.created_by
        WHERE {where_clause}
        ORDER BY b.created_at DESC
        LIMIT :limit OFFSET :offset
    """)

    result = await db.execute(query, params)

    bills = []
    for row in result:
        bills.append({
            "id": str(row[0]),
            "bill_number": row[1],
            "bill_date": row[2].isoformat() if row[2] else None,
            "total_amount": float(row[3]) if row[3] else 0,
            "discount": float(row[4]) if row[4] else 0,
            "net_amount": float(row[5]) if row[5] else 0,
            "status": row[6],
            "is_credit": row[7],
            "amount_paid": float(row[8]) if row[8] else 0,
            "balance_due": float(row[9]) if row[9] else 0,
            "due_date": row[10].isoformat() if row[10] else None,
            "created_at": row[11].isoformat() if row[11] else None,
            "supplier": {
                "id": str(row[12]) if row[12] else None,
                "name": row[13]
            },
            "created_by": row[14]
        })

    count_query = text(f"SELECT COUNT(*) FROM bills b WHERE {where_clause}")
    count_result = await db.execute(count_query, params)
    total = count_result.scalar() or 0

    return {
        "success": True,
        "data": bills,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "pages": (total + limit - 1) // limit
        }
    }


@app.get("/api/bills/{bill_id}")
async def get_bill(
        bill_id: str,
        pharmacy_id: str,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """Get single purchase bill with all details"""

    # Get bill header
    bill_query = text("""
        SELECT b.id, b.bill_number, b.bill_date, b.total_amount, b.discount,
               b.net_amount, b.status, b.is_credit, b.amount_paid, b.balance_due,
               b.due_date, b.created_at, b.updated_at,
               s.id as supplier_id, s.name as supplier_name, s.contact_person, s.phone,
               u.fullname as created_by_name
        FROM bills b
        LEFT JOIN suppliers s ON s.id = b.supplier_id
        LEFT JOIN users u ON u.id = b.created_by
        WHERE b.id = :bill_id AND b.pharmacy_id = :pharmacy_id AND b.is_deleted = FALSE
    """)

    result = await db.execute(bill_query, {
        "bill_id": UUID(bill_id),
        "pharmacy_id": UUID(pharmacy_id)
    })
    bill_row = result.fetchone()

    if not bill_row:
        return {"success": False, "error": "Bill not found"}

    # Get bill details
    details_query = text("""
        SELECT bd.id, bd.quantity, bd.purchase_price, bd.selling_price, bd.discount,
               bd.batch_number, bd.expiry_date, bd.created_at,
               m.id as medicine_id, m.name as medicine_name, m.brand, m.strength,
               bd.unit_type_id, ut.name as unit_type_name,
               bd.pack_size, bd.subunit_size, bd.smallest_unit_factor,
               batch.id as batch_id, batch.quantity_remaining as batch_stock
        FROM bill_details bd
        LEFT JOIN medicines m ON m.id = bd.medicine_id
        LEFT JOIN unit_types ut ON ut.id = bd.unit_type_id
        LEFT JOIN batches batch ON batch.medicine_id = bd.medicine_id 
            AND batch.batch_number = bd.batch_number AND batch.is_deleted = FALSE
        WHERE bd.bill_id = :bill_id AND bd.is_deleted = FALSE
        ORDER BY bd.created_at
    """)

    details_result = await db.execute(details_query, {"bill_id": UUID(bill_id)})

    details = []
    for row in details_result:
        details.append({
            "id": str(row[0]),
            "quantity": row[1],
            "purchase_price": float(row[2]) if row[2] else 0,
            "selling_price": float(row[3]) if row[3] else 0,
            "discount": float(row[4]) if row[4] else 0,
            "batch_number": row[5],
            "expiry_date": row[6].isoformat() if row[6] else None,
            "created_at": row[7].isoformat() if row[7] else None,
            "medicine": {
                "id": str(row[8]) if row[8] else None,
                "name": row[9],
                "brand": row[10],
                "strength": row[11]
            },
            "unit_type": {
                "id": str(row[12]) if row[12] else None,
                "name": row[13]
            },
            "packaging": {
                "pack_size": row[14],
                "subunit_size": row[15],
                "smallest_unit_factor": row[16]
            },
            "batch_id": str(row[17]) if row[17] else None,
            "current_stock": row[18] if row[18] else 0
        })

    bill = {
        "id": str(bill_row[0]),
        "bill_number": bill_row[1],
        "bill_date": bill_row[2].isoformat() if bill_row[2] else None,
        "total_amount": float(bill_row[3]) if bill_row[3] else 0,
        "discount": float(bill_row[4]) if bill_row[4] else 0,
        "net_amount": float(bill_row[5]) if bill_row[5] else 0,
        "status": bill_row[6],
        "is_credit": bill_row[7],
        "amount_paid": float(bill_row[8]) if bill_row[8] else 0,
        "balance_due": float(bill_row[9]) if bill_row[9] else 0,
        "due_date": bill_row[10].isoformat() if bill_row[10] else None,
        "created_at": bill_row[11].isoformat() if bill_row[11] else None,
        "updated_at": bill_row[12].isoformat() if bill_row[12] else None,
        "supplier": {
            "id": str(bill_row[13]) if bill_row[13] else None,
            "name": bill_row[14],
            "contact_person": bill_row[15],
            "phone": bill_row[16]
        },
        "created_by": bill_row[17],
        "details": details,
        "item_count": len(details)
    }

    return {"success": True, "data": bill}


class BillItemRequest(BaseModel):
    medicine_id: str
    batch_number: str
    quantity: int
    purchase_price: float
    selling_price: float
    discount: float = 0
    expiry_date: Optional[str] = None
    unit_type_id: Optional[str] = None
    pack_size: Optional[int] = None
    subunit_size: Optional[int] = None
    smallest_unit_factor: Optional[int] = 1


class CreateBillRequest(BaseModel):
    supplier_id: str
    bill_number: Optional[str] = None
    bill_date: Optional[str] = None
    discount: float = 0
    is_credit: bool = False
    due_date: Optional[str] = None
    items: List[BillItemRequest]


@app.post("/api/bills")
async def create_bill(
        request: CreateBillRequest,
        pharmacy_id: str,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """Create a new purchase bill and update inventory"""
    user_id = current_user.get("sub")

    # Validate supplier exists
    supplier_check = await db.execute(
        text("SELECT id FROM suppliers WHERE id = :id AND pharmacy_id = :pharmacy_id AND is_deleted = FALSE"),
        {"id": UUID(request.supplier_id), "pharmacy_id": UUID(pharmacy_id)}
    )
    if not supplier_check.fetchone():
        return {"success": False, "error": "Supplier not found"}

    if not request.items:
        return {"success": False, "error": "At least one item is required"}

    # Calculate totals
    total_amount = 0
    bill_items = []

    for item in request.items:
        if item.quantity <= 0:
            return {"success": False, "error": "Quantity must be positive"}

        medicine_check = await db.execute(
            text("SELECT id FROM medicines WHERE id = :id AND pharmacy_id = :pharmacy_id AND is_deleted = FALSE"),
            {"id": UUID(item.medicine_id), "pharmacy_id": UUID(pharmacy_id)}
        )
        if not medicine_check.fetchone():
            return {"success": False, "error": f"Medicine {item.medicine_id} not found"}

        item_total = item.quantity * item.purchase_price * (1 - item.discount / 100)
        total_amount += item_total

        bill_items.append({
            "medicine_id": UUID(item.medicine_id),
            "batch_number": item.batch_number,
            "quantity": item.quantity,
            "purchase_price": item.purchase_price,
            "selling_price": item.selling_price,
            "discount": item.discount,
            "expiry_date": datetime.strptime(item.expiry_date, "%Y-%m-%d").date() if item.expiry_date else None,
            "unit_type_id": UUID(item.unit_type_id) if item.unit_type_id else None,
            "pack_size": item.pack_size,
            "subunit_size": item.subunit_size,
            "smallest_unit_factor": item.smallest_unit_factor or 1,
            "item_total": item_total
        })

    net_amount = total_amount - request.discount

    # Generate bill number if not provided
    if not request.bill_number:
        bill_number = f"BILL-{datetime.now().strftime('%Y%m%d%H%M%S')}-{str(uuid.uuid4())[:8]}"
    else:
        bill_number = request.bill_number

    bill_date = datetime.strptime(request.bill_date, "%Y-%m-%d") if request.bill_date else datetime.now()
    due_date = datetime.strptime(request.due_date, "%Y-%m-%d").date() if request.due_date else None

    status = 'Pending' if request.is_credit else 'Paid'
    balance_due = net_amount if request.is_credit else 0

    # Create bill
    bill_id = uuid.uuid4()
    await db.execute(
        text("""
            INSERT INTO bills (id, pharmacy_id, supplier_id, bill_number, bill_date,
                total_amount, discount, net_amount, status, is_credit, amount_paid,
                balance_due, due_date, created_by, source)
            VALUES (:id, :pharmacy_id, :supplier_id, :bill_number, :bill_date,
                :total_amount, :discount, :net_amount, :status, :is_credit, 0,
                :balance_due, :due_date, :created_by, 'mobile')
        """),
        {
            "id": bill_id,
            "pharmacy_id": UUID(pharmacy_id),
            "supplier_id": UUID(request.supplier_id),
            "bill_number": bill_number,
            "bill_date": bill_date,
            "total_amount": total_amount,
            "discount": request.discount,
            "net_amount": net_amount,
            "status": status,
            "is_credit": request.is_credit,
            "balance_due": balance_due,
            "due_date": due_date,
            "created_by": UUID(user_id)
        }
    )

    # Create bill details and update/create batches
    for item in bill_items:
        detail_id = uuid.uuid4()
        await db.execute(
            text("""
                INSERT INTO bill_details (id, bill_id, medicine_id, batch_number, quantity,
                    purchase_price, selling_price, discount, expiry_date,
                    unit_type_id, pack_size, subunit_size, smallest_unit_factor)
                VALUES (:id, :bill_id, :medicine_id, :batch_number, :quantity,
                    :purchase_price, :selling_price, :discount, :expiry_date,
                    :unit_type_id, :pack_size, :subunit_size, :smallest_unit_factor)
            """),
            {
                "id": detail_id,
                "bill_id": bill_id,
                "medicine_id": item["medicine_id"],
                "batch_number": item["batch_number"],
                "quantity": item["quantity"],
                "purchase_price": item["purchase_price"],
                "selling_price": item["selling_price"],
                "discount": item["discount"],
                "expiry_date": item["expiry_date"],
                "unit_type_id": item["unit_type_id"],
                "pack_size": item["pack_size"],
                "subunit_size": item["subunit_size"],
                "smallest_unit_factor": item["smallest_unit_factor"]
            }
        )

        # Check if batch exists
        batch_check = await db.execute(
            text("""
                SELECT id, quantity_received, quantity_remaining 
                FROM batches 
                WHERE medicine_id = :medicine_id AND batch_number = :batch_number 
                    AND pharmacy_id = :pharmacy_id AND is_deleted = FALSE
            """),
            {
                "medicine_id": item["medicine_id"],
                "batch_number": item["batch_number"],
                "pharmacy_id": UUID(pharmacy_id)
            }
        )
        existing_batch = batch_check.fetchone()

        if existing_batch:
            # Update existing batch
            await db.execute(
                text("""
                    UPDATE batches 
                    SET quantity_received = quantity_received + :quantity,
                        quantity_remaining = quantity_remaining + :quantity,
                        purchase_price = :purchase_price,
                        selling_price = :selling_price,
                        expiry_date = COALESCE(:expiry_date, expiry_date),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :batch_id
                """),
                {
                    "quantity": item["quantity"],
                    "purchase_price": item["purchase_price"],
                    "selling_price": item["selling_price"],
                    "expiry_date": item["expiry_date"],
                    "batch_id": existing_batch[0]
                }
            )
        else:
            # Create new batch
            new_batch_id = uuid.uuid4()
            await db.execute(
                text("""
                    INSERT INTO batches (id, pharmacy_id, medicine_id, batch_number,
                        purchase_price, selling_price, quantity_received, quantity_remaining,
                        expiry_date, created_by, source)
                    VALUES (:id, :pharmacy_id, :medicine_id, :batch_number,
                        :purchase_price, :selling_price, :quantity, :quantity,
                        :expiry_date, :created_by, 'mobile')
                """),
                {
                    "id": new_batch_id,
                    "pharmacy_id": UUID(pharmacy_id),
                    "medicine_id": item["medicine_id"],
                    "batch_number": item["batch_number"],
                    "purchase_price": item["purchase_price"],
                    "selling_price": item["selling_price"],
                    "quantity": item["quantity"],
                    "expiry_date": item["expiry_date"],
                    "created_by": UUID(user_id)
                }
            )

            # Create batch_units if unit_type provided
            if item["unit_type_id"]:
                batch_unit_id = uuid.uuid4()
                await db.execute(
                    text("""
                        INSERT INTO batch_units (id, pharmacy_id, batch_id, unit_type_id,
                            pack_size, subunit_size, smallest_unit_factor,
                            purchase_price_per_unit, selling_price_per_unit, source)
                        VALUES (:id, :pharmacy_id, :batch_id, :unit_type_id,
                            :pack_size, :subunit_size, :smallest_unit_factor,
                            :purchase_price, :selling_price, 'mobile')
                    """),
                    {
                        "id": batch_unit_id,
                        "pharmacy_id": UUID(pharmacy_id),
                        "batch_id": new_batch_id,
                        "unit_type_id": item["unit_type_id"],
                        "pack_size": item["pack_size"],
                        "subunit_size": item["subunit_size"],
                        "smallest_unit_factor": item["smallest_unit_factor"],
                        "purchase_price": item["purchase_price"],
                        "selling_price": item["selling_price"]
                    }
                )

    # Update supplier balance if credit
    if request.is_credit:
        await db.execute(
            text("""
                UPDATE suppliers 
                SET balance = balance + :amount,
                    total_purchases = total_purchases + :amount,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :supplier_id
            """),
            {"amount": net_amount, "supplier_id": UUID(request.supplier_id)}
        )

    await db.commit()

    return {
        "success": True,
        "data": {
            "id": str(bill_id),
            "bill_number": bill_number,
            "net_amount": net_amount,
            "balance_due": balance_due,
            "created_at": bill_date.isoformat()
        }
    }


class UpdateBillRequest(BaseModel):
    status: Optional[str] = None  # 'Paid', 'Pending', 'Partial', 'Void'
    discount: Optional[float] = None


@app.put("/api/bills/{bill_id}")
async def update_bill(
        bill_id: str,
        request: UpdateBillRequest,
        pharmacy_id: str,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """Update bill status or discount"""

    bill_check = await db.execute(
        text(
            "SELECT id, status, net_amount, supplier_id, is_credit FROM bills WHERE id = :id AND pharmacy_id = :pharmacy_id AND is_deleted = FALSE"),
        {"id": UUID(bill_id), "pharmacy_id": UUID(pharmacy_id)}
    )
    bill = bill_check.fetchone()

    if not bill:
        return {"success": False, "error": "Bill not found"}

    updates = []
    params = {"id": UUID(bill_id)}

    if request.status:
        updates.append("status = :status")
        params["status"] = request.status

        # If marking as Paid, update balance_due to 0
        if request.status == 'Paid' and bill[4]:  # is_credit
            updates.append("balance_due = 0")
            updates.append("amount_paid = net_amount")

    if request.discount is not None:
        updates.append("discount = :discount")
        params["discount"] = request.discount
        # Recalculate net_amount
        await db.execute(
            text("UPDATE bills SET net_amount = total_amount - :discount WHERE id = :id"),
            {"discount": request.discount, "id": UUID(bill_id)}
        )

    if updates:
        updates.append("updated_at = CURRENT_TIMESTAMP")
        updates.append("sync_version = sync_version + 1")
        await db.execute(
            text(f"UPDATE bills SET {', '.join(updates)} WHERE id = :id"),
            params
        )

    await db.commit()

    return {"success": True, "message": "Bill updated"}


# ============================================
# DELETE BILL (Soft Delete)
# ============================================
@app.delete("/api/bills/{bill_id}")
async def delete_bill(
        bill_id: str,
        pharmacy_id: str,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """Soft delete a bill"""

    # Check if bill has returns
    returns_check = await db.execute(
        text("SELECT COUNT(*) FROM supplier_returns WHERE bill_id = :bill_id AND is_deleted = FALSE"),
        {"bill_id": UUID(bill_id)}
    )
    return_count = returns_check.scalar() or 0

    if return_count > 0:
        return {"success": False, "error": f"Cannot delete bill with {return_count} associated returns"}

    await db.execute(
        text(
            "UPDATE bills SET is_deleted = TRUE, updated_at = CURRENT_TIMESTAMP WHERE id = :id AND pharmacy_id = :pharmacy_id"),
        {"id": UUID(bill_id), "pharmacy_id": UUID(pharmacy_id)}
    )

    await db.commit()

    return {"success": True, "message": "Bill deleted"}









# ============================================
# ENHANCED SUPPLIER SEARCH
# ============================================
@app.get("/api/suppliers/search")
async def search_suppliers(
        pharmacy_id: str,
        q: str,
        limit: int = 20,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """Quick search suppliers by name or contact"""

    result = await db.execute(
        text("""
            SELECT id, name, contact_person, phone
            FROM suppliers
            WHERE pharmacy_id = :pharmacy_id
                AND is_deleted = FALSE
                AND (name ILIKE :search OR contact_person ILIKE :search OR phone ILIKE :search)
            ORDER BY name
            LIMIT :limit
        """),
        {"pharmacy_id": UUID(pharmacy_id), "search": f"%{q}%", "limit": limit}
    )

    suppliers = []
    for row in result:
        suppliers.append({
            "id": str(row[0]),
            "name": row[1],
            "contact_person": row[2],
            "phone": row[3]
        })

    return {"success": True, "data": suppliers}


# ============================================
# UPDATE CUSTOMER RETURN
# ============================================
class UpdateReturnRequest(BaseModel):
    reason: Optional[str] = None
    total_amount: Optional[float] = None

@app.put("/api/customer-returns/{return_id}")
async def update_customer_return(
        return_id: str,
        request: UpdateReturnRequest,
        pharmacy_id: str,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """Update customer return reason or amount"""

    updates = []
    params = {"id": UUID(return_id), "pharmacy_id": UUID(pharmacy_id)}

    if request.reason:
        updates.append("reason = :reason")
        params["reason"] = request.reason
    if request.total_amount is not None:
        updates.append("total_amount = :total_amount")
        params["total_amount"] = request.total_amount

    if not updates:
        return {"success": False, "error": "No fields to update"}

    updates.append("updated_at = CURRENT_TIMESTAMP")
    updates.append("sync_version = sync_version + 1")  # ← ADD THIS

    await db.execute(
        text(f"UPDATE customer_returns SET {', '.join(updates)} WHERE id = :id AND pharmacy_id = :pharmacy_id"),
        params
    )

    await db.commit()

    return {"success": True, "message": "Return updated"}


# ============================================
# DELETE CUSTOMER RETURN
# ============================================
@app.delete("/api/customer-returns/{return_id}")
async def delete_customer_return(
        return_id: str,
        pharmacy_id: str,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """Soft delete a customer return"""

    # First restore stock if needed
    result = await db.execute(
        text("""
            SELECT cr.invoice_id, crd.batch_id, crd.quantity
            FROM customer_returns cr
            JOIN customer_return_details crd ON crd.return_id = cr.id
            WHERE cr.id = :id AND cr.pharmacy_id = :pharmacy_id
        """),
        {"id": UUID(return_id), "pharmacy_id": UUID(pharmacy_id)}
    )

    for row in result:
        # Reduce stock again (since return added stock)
        await db.execute(
            text("UPDATE batches SET quantity_remaining = quantity_remaining - :qty WHERE id = :batch_id"),
            {"qty": row[2], "batch_id": row[1]}
        )

    await db.execute(
        text("""
            UPDATE customer_returns 
            SET is_deleted = TRUE, updated_at = CURRENT_TIMESTAMP
            WHERE id = :id AND pharmacy_id = :pharmacy_id
        """),
        {"id": UUID(return_id), "pharmacy_id": UUID(pharmacy_id)}
    )

    await db.commit()

    return {"success": True, "message": "Return deleted"}


# ============================================
# UPDATE SUPPLIER RETURN
# ============================================
@app.put("/api/supplier-returns/{return_id}")
async def update_supplier_return(
        return_id: str,
        request: UpdateReturnRequest,
        pharmacy_id: str,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """Update supplier return"""

    updates = []
    params = {"id": UUID(return_id), "pharmacy_id": UUID(pharmacy_id)}

    if request.reason:
        updates.append("reason = :reason")
        params["reason"] = request.reason
    if request.total_amount is not None:
        updates.append("total_amount = :total_amount")
        params["total_amount"] = request.total_amount

    if not updates:
        return {"success": False, "error": "No fields to update"}

    updates.append("updated_at = CURRENT_TIMESTAMP")
    updates.append("sync_version = sync_version + 1")  # ← ADD THIS

    await db.execute(
        text(f"UPDATE supplier_returns SET {', '.join(updates)} WHERE id = :id AND pharmacy_id = :pharmacy_id"),
        params
    )

    await db.commit()

    return {"success": True, "message": "Supplier return updated"}


# ============================================
# DELETE SUPPLIER RETURN
# ============================================
@app.delete("/api/supplier-returns/{return_id}")
async def delete_supplier_return(
        return_id: str,
        pharmacy_id: str,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """Soft delete a supplier return"""

    # Restore stock (since return reduced stock)
    result = await db.execute(
        text("""
            SELECT srd.batch_id, srd.quantity
            FROM supplier_returns sr
            JOIN supplier_return_details srd ON srd.return_id = sr.id
            WHERE sr.id = :id AND sr.pharmacy_id = :pharmacy_id
        """),
        {"id": UUID(return_id), "pharmacy_id": UUID(pharmacy_id)}
    )

    for row in result:
        await db.execute(
            text("UPDATE batches SET quantity_remaining = quantity_remaining + :qty WHERE id = :batch_id"),
            {"qty": row[1], "batch_id": row[0]}
        )

    await db.execute(
        text("""
            UPDATE supplier_returns 
            SET is_deleted = TRUE, updated_at = CURRENT_TIMESTAMP
            WHERE id = :id AND pharmacy_id = :pharmacy_id
        """),
        {"id": UUID(return_id), "pharmacy_id": UUID(pharmacy_id)}
    )

    await db.commit()

    return {"success": True, "message": "Supplier return deleted"}


# ============================================
# GET CUSTOMER RETURNS (with search)
# ============================================
@app.get("/api/customer-returns")
async def get_customer_returns(
        pharmacy_id: str,
        invoice_id: Optional[str] = None,
        customer_id: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        page: int = 1,
        limit: int = 20,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """Get customer returns with filters"""

    offset = (page - 1) * limit

    conditions = ["cr.pharmacy_id = :pharmacy_id", "cr.is_deleted = FALSE"]
    params = {"pharmacy_id": UUID(pharmacy_id), "limit": limit, "offset": offset}

    if invoice_id:
        conditions.append("cr.invoice_id = :invoice_id")
        params["invoice_id"] = UUID(invoice_id)
    if customer_id:
        conditions.append("cr.customer_id = :customer_id")
        params["customer_id"] = UUID(customer_id)
    if start_date:
        conditions.append("cr.return_date >= :start_date")
        params["start_date"] = start_date
    if end_date:
        conditions.append("cr.return_date <= :end_date")
        params["end_date"] = end_date

    where_clause = " AND ".join(conditions)

    query = text(f"""
        SELECT cr.id, cr.return_date, cr.total_amount, cr.reason,
               i.invoice_number, c.full_name as customer_name
        FROM customer_returns cr
        LEFT JOIN invoices i ON i.id = cr.invoice_id
        LEFT JOIN customers c ON c.id = cr.customer_id
        WHERE {where_clause}
        ORDER BY cr.return_date DESC
        LIMIT :limit OFFSET :offset
    """)

    result = await db.execute(query, params)

    returns = []
    for row in result:
        returns.append({
            "id": str(row[0]),
            "return_date": row[1].isoformat() if row[1] else None,
            "total_amount": float(row[2]),
            "reason": row[3],
            "invoice_number": row[4],
            "customer_name": row[5]
        })

    count_query = text(f"SELECT COUNT(*) FROM customer_returns cr WHERE {where_clause}")
    count_result = await db.execute(count_query, params)
    total = count_result.scalar() or 0

    return {
        "success": True,
        "data": returns,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "pages": (total + limit - 1) // limit
        }
    }


# ============================================
# GET SUPPLIER RETURNS (with search)
# ============================================
@app.get("/api/supplier-returns")
async def get_supplier_returns(
        pharmacy_id: str,
        bill_id: Optional[str] = None,
        supplier_id: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        page: int = 1,
        limit: int = 20,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """Get supplier returns with filters"""

    offset = (page - 1) * limit

    conditions = ["sr.pharmacy_id = :pharmacy_id", "sr.is_deleted = FALSE"]
    params = {"pharmacy_id": UUID(pharmacy_id), "limit": limit, "offset": offset}

    if bill_id:
        conditions.append("sr.bill_id = :bill_id")
        params["bill_id"] = UUID(bill_id)
    if supplier_id:
        conditions.append("sr.supplier_id = :supplier_id")
        params["supplier_id"] = UUID(supplier_id)
    if start_date:
        conditions.append("sr.return_date >= :start_date")
        params["start_date"] = start_date
    if end_date:
        conditions.append("sr.return_date <= :end_date")
        params["end_date"] = end_date

    where_clause = " AND ".join(conditions)

    query = text(f"""
        SELECT sr.id, sr.return_date, sr.total_amount, sr.reason,
               b.bill_number, s.name as supplier_name
        FROM supplier_returns sr
        LEFT JOIN bills b ON b.id = sr.bill_id
        LEFT JOIN suppliers s ON s.id = sr.supplier_id
        WHERE {where_clause}
        ORDER BY sr.return_date DESC
        LIMIT :limit OFFSET :offset
    """)

    result = await db.execute(query, params)

    returns = []
    for row in result:
        returns.append({
            "id": str(row[0]),
            "return_date": row[1].isoformat() if row[1] else None,
            "total_amount": float(row[2]),
            "reason": row[3],
            "bill_number": row[4],
            "supplier_name": row[5]
        })

    count_query = text(f"SELECT COUNT(*) FROM supplier_returns sr WHERE {where_clause}")
    count_result = await db.execute(count_query, params)
    total = count_result.scalar() or 0

    return {
        "success": True,
        "data": returns,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "pages": (total + limit - 1) // limit
        }
    }







# ============================================
# SALES - Create New Sale
# ============================================
from pydantic import BaseModel
from typing import List, Optional


class SaleItem(BaseModel):
    batch_id: str
    quantity: int
    selling_price: float


class CreateSaleRequest(BaseModel):
    customer_id: Optional[str] = None
    items: List[SaleItem]
    discount: float = 0
    payment_method: str = "cash"  # cash, card, mobile_money


@app.post("/api/sales")
async def create_sale(
        request: CreateSaleRequest,
        pharmacy_id: str,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """
    Create a new sale, reduce stock automatically
    """
    user_id = current_user.get("sub")

    # Calculate totals
    total_amount = 0
    sale_items = []

    for item in request.items:
        if item.quantity <= 0:
            return {"success": False, "error": f"Quantity must be positive, got {item.quantity}"}
        # Get batch details
        batch_result = await db.execute(
            text("""
                SELECT medicine_id, selling_price, quantity_remaining, batch_number
                FROM batches
                WHERE id = :batch_id AND pharmacy_id = :pharmacy_id AND is_deleted = FALSE
            """),
            {"batch_id": UUID(item.batch_id), "pharmacy_id": UUID(pharmacy_id)}
        )
        batch = batch_result.fetchone()

        if not batch:
            return {"success": False, "error": f"Batch {item.batch_id} not found"}

        if batch[2] < item.quantity:
            return {"success": False, "error": f"Insufficient stock for batch {batch[3]}"}

        item_total = item.quantity * item.selling_price
        total_amount += item_total

        sale_items.append({
            "batch_id": UUID(item.batch_id),
            "medicine_id": batch[0],
            "quantity": item.quantity,
            "unit_price": item.selling_price,
            "total": item_total
        })

    # Calculate net amount
    net_amount = total_amount - request.discount

    # Generate invoice number
    invoice_number = f"INV-{datetime.now().strftime('%Y%m%d%H%M%S')}-{str(uuid.uuid4())[:8]}"

    # Create invoice
    invoice_id = uuid.uuid4()
    await db.execute(
        text("""
            INSERT INTO invoices (id, pharmacy_id, customer_id, invoice_number, 
                total_amount, discount, net_amount, status, created_by, source)
            VALUES (:id, :pharmacy_id, :customer_id, :invoice_number,
                :total_amount, :discount, :net_amount, 'Paid', :created_by, 'mobile')
        """),
        {
            "id": invoice_id,
            "pharmacy_id": UUID(pharmacy_id),
            "customer_id": UUID(request.customer_id) if request.customer_id else None,
            "invoice_number": invoice_number,
            "total_amount": total_amount,
            "discount": request.discount,
            "net_amount": net_amount,
            "created_by": UUID(user_id)
        }
    )

    # Create invoice details and reduce stock
    for item in sale_items:
        # Add invoice detail
        await db.execute(
            text("""
                INSERT INTO invoice_details (id, invoice_id, batch_id, quantity, unit_price)
                VALUES (gen_random_uuid(), :invoice_id, :batch_id, :quantity, :unit_price)
            """),
            {
                "invoice_id": invoice_id,
                "batch_id": item["batch_id"],
                "quantity": item["quantity"],
                "unit_price": item["unit_price"]
            }
        )

        # Reduce stock (trigger will auto-log to change_log)
        await db.execute(
            text("""
                UPDATE batches 
                SET quantity_remaining = quantity_remaining - :quantity,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :batch_id
            """),
            {"quantity": item["quantity"], "batch_id": item["batch_id"]}
        )

    await db.commit()

    return {
        "success": True,
        "data": {
            "invoice_id": str(invoice_id),
            "invoice_number": invoice_number,
            "total_amount": total_amount,
            "discount": request.discount,
            "net_amount": net_amount,
            "created_at": datetime.now().isoformat()
        }
    }


# ============================================
# CUSTOMERS - List with Balances
# ============================================
@app.get("/api/customers")
async def get_customers(
        pharmacy_id: str,
        search: str = "",
        page: int = 1,
        limit: int = 20,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """
    Get customers with credit balance
    """
    offset = (page - 1) * limit

    search_condition = ""
    params = {"pharmacy_id": UUID(pharmacy_id), "limit": limit, "offset": offset}

    if search:
        search_condition = "AND (full_name ILIKE :search OR phone ILIKE :search)"
        params["search"] = f"%{search}%"

    query = text(f"""
        SELECT 
            id,
            full_name,
            phone,
            address,
            balance,
            credit_limit,
            total_purchases,
            created_at
        FROM customers
        WHERE pharmacy_id = :pharmacy_id
            AND is_deleted = FALSE
            {search_condition}
        ORDER BY full_name
        LIMIT :limit OFFSET :offset
    """)

    result = await db.execute(query, params)

    customers = []
    for row in result:
        customers.append({
            "id": str(row[0]),
            "full_name": row[1],
            "phone": row[2],
            "address": row[3],
            "balance": float(row[4] or 0),
            "credit_limit": float(row[5] or 0),
            "total_purchases": float(row[6] or 0),
            "created_at": row[7].isoformat() if row[7] else None
        })

    # Get total count
    count_query = text(f"""
        SELECT COUNT(*)
        FROM customers
        WHERE pharmacy_id = :pharmacy_id AND is_deleted = FALSE
        {search_condition}
    """)
    count_result = await db.execute(count_query, params)
    total = count_result.scalar() or 0

    return {
        "success": True,
        "data": customers,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "pages": (total + limit - 1) // limit
        }
    }


# ============================================
# CREATE/UPDATE CUSTOMER
# ============================================
class CreateCustomerRequest(BaseModel):
    full_name: str
    phone: Optional[str] = None
    address: Optional[str] = None
    credit_limit: float = 0


@app.post("/api/customers")
async def create_customer(
        request: CreateCustomerRequest,
        pharmacy_id: str,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """
    Add a new customer with validation
    """
    # Validation: Customer name is required
    if not request.full_name or not request.full_name.strip():
        return {"success": False, "error": "Customer name is required"}

    # Optional: Validate phone format (basic)
    if request.phone and len(request.phone) < 9:
        return {"success": False, "error": "Phone number must be at least 9 digits"}

    customer_id = uuid.uuid4()

    await db.execute(
        text("""
            INSERT INTO customers (id, pharmacy_id, full_name, phone, address, credit_limit, created_by, source)
            VALUES (:id, :pharmacy_id, :full_name, :phone, :address, :credit_limit, :created_by, 'mobile')
        """),
        {
            "id": customer_id,
            "pharmacy_id": UUID(pharmacy_id),
            "full_name": request.full_name.strip(),
            "phone": request.phone,
            "address": request.address,
            "credit_limit": request.credit_limit,
            "created_by": UUID(current_user.get("sub"))
        }
    )

    await db.commit()

    return {
        "success": True,
        "data": {"id": str(customer_id)}
    }

# ============================================
# SUPPLIERS - List
# ============================================
@app.get("/api/suppliers")
async def get_suppliers(
        pharmacy_id: str,
        search: str = "",
        page: int = 1,
        limit: int = 20,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """
    Get suppliers list
    """
    offset = (page - 1) * limit

    search_condition = ""
    params = {"pharmacy_id": UUID(pharmacy_id), "limit": limit, "offset": offset}

    if search:
        search_condition = "AND (name ILIKE :search OR contact_person ILIKE :search OR phone ILIKE :search)"
        params["search"] = f"%{search}%"

    query = text(f"""
        SELECT 
            id,
            name,
            contact_person,
            phone,
            email,
            address,
            total_purchases,
            created_at
        FROM suppliers
        WHERE pharmacy_id = :pharmacy_id
            AND is_deleted = FALSE
            {search_condition}
        ORDER BY name
        LIMIT :limit OFFSET :offset
    """)

    result = await db.execute(query, params)

    suppliers = []
    for row in result:
        suppliers.append({
            "id": str(row[0]),
            "name": row[1],
            "contact_person": row[2],
            "phone": row[3],
            "email": row[4],
            "address": row[5],
            "total_purchases": float(row[6] or 0),
            "created_at": row[7].isoformat() if row[7] else None
        })

    # Get total count
    count_query = text(f"""
        SELECT COUNT(*)
        FROM suppliers
        WHERE pharmacy_id = :pharmacy_id AND is_deleted = FALSE
        {search_condition}
    """)
    count_result = await db.execute(count_query, params)
    total = count_result.scalar() or 0

    return {
        "success": True,
        "data": suppliers,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "pages": (total + limit - 1) // limit
        }
    }


# ============================================
# CREATE SUPPLIER
# ============================================
class CreateSupplierRequest(BaseModel):
    name: str
    contact_person: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None


@app.post("/api/suppliers")
async def create_supplier(
        request: CreateSupplierRequest,
        pharmacy_id: str,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """
    Add a new supplier
    """
    supplier_id = uuid.uuid4()

    await db.execute(
        text("""
            INSERT INTO suppliers (id, pharmacy_id, name, contact_person, phone, email, address, created_by, source)
            VALUES (:id, :pharmacy_id, :name, :contact_person, :phone, :email, :address, :created_by, 'mobile')
        """),
        {
            "id": supplier_id,
            "pharmacy_id": UUID(pharmacy_id),
            "name": request.name,
            "contact_person": request.contact_person,
            "phone": request.phone,
            "email": request.email,
            "address": request.address,
            "created_by": UUID(current_user.get("sub"))
        }
    )

    await db.commit()

    return {
        "success": True,
        "data": {"id": str(supplier_id)}
    }


# ============================================
# CUSTOMER RETURNS - Process Return
# ============================================
class CustomerReturnRequest(BaseModel):
    invoice_id: str
    items: List[SaleItem]
    reason: Optional[str] = None


@app.post("/api/customer-returns")
async def create_customer_return(
        request: CustomerReturnRequest,
        pharmacy_id: str,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """
    Process a customer return
    Adds stock back to inventory
    """
    user_id = current_user.get("sub")
    total_amount = 0

    # Verify invoice belongs to this pharmacy
    invoice_check = await db.execute(
        text("SELECT id, customer_id FROM invoices WHERE id = :invoice_id AND pharmacy_id = :pharmacy_id"),
        {"invoice_id": UUID(request.invoice_id), "pharmacy_id": UUID(pharmacy_id)}
    )
    invoice = invoice_check.fetchone()

    if not invoice:
        return {"success": False, "error": "Invoice not found"}

    # Process each returned item
    for item in request.items:
        # Get batch details
        batch_result = await db.execute(
            text("""
                SELECT medicine_id, selling_price, quantity_remaining
                FROM batches
                WHERE id = :batch_id AND pharmacy_id = :pharmacy_id
            """),
            {"batch_id": UUID(item.batch_id), "pharmacy_id": UUID(pharmacy_id)}
        )
        batch = batch_result.fetchone()

        if not batch:
            return {"success": False, "error": f"Batch {item.batch_id} not found"}

        item_total = item.quantity * item.selling_price
        total_amount += item_total

        # Add stock back (trigger will auto-log)
        await db.execute(
            text("""
                UPDATE batches 
                SET quantity_remaining = quantity_remaining + :quantity,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :batch_id
            """),
            {"quantity": item.quantity, "batch_id": UUID(item.batch_id)}
        )

    # Create return record
    return_id = uuid.uuid4()
    await db.execute(
        text("""
            INSERT INTO customer_returns (id, pharmacy_id, invoice_id, customer_id, 
                total_amount, reason, created_by, source)
            VALUES (:id, :pharmacy_id, :invoice_id, :customer_id, 
                :total_amount, :reason, :created_by, 'mobile')
        """),
        {
            "id": return_id,
            "pharmacy_id": UUID(pharmacy_id),
            "invoice_id": UUID(request.invoice_id),
            "customer_id": invoice[1],
            "total_amount": total_amount,
            "reason": request.reason,
            "created_by": UUID(user_id)
        }
    )

    await db.commit()

    return {
        "success": True,
        "data": {
            "return_id": str(return_id),
            "total_amount": total_amount,
            "message": "Return processed successfully"
        }
    }


# ============================================
# SUPPLIER RETURNS - Return to Supplier
# ============================================
class SupplierReturnRequest(BaseModel):
    bill_id: str
    items: List[SaleItem]
    reason: Optional[str] = None


@app.post("/api/supplier-returns")
async def create_supplier_return(
        request: SupplierReturnRequest,
        pharmacy_id: str,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """
    Return items to supplier
    Reduces stock from inventory
    """
    user_id = current_user.get("sub")
    total_amount = 0

    # Verify bill belongs to this pharmacy
    bill_check = await db.execute(
        text("SELECT id, supplier_id FROM bills WHERE id = :bill_id AND pharmacy_id = :pharmacy_id"),
        {"bill_id": UUID(request.bill_id), "pharmacy_id": UUID(pharmacy_id)}
    )
    bill = bill_check.fetchone()

    if not bill:
        return {"success": False, "error": "Bill not found"}

    # Process each returned item
    for item in request.items:
        # Get batch details
        batch_result = await db.execute(
            text("""
                SELECT medicine_id, selling_price, quantity_remaining
                FROM batches
                WHERE id = :batch_id AND pharmacy_id = :pharmacy_id
            """),
            {"batch_id": UUID(item.batch_id), "pharmacy_id": UUID(pharmacy_id)}
        )
        batch = batch_result.fetchone()

        if not batch:
            return {"success": False, "error": f"Batch {item.batch_id} not found"}

        if batch[2] < item.quantity:
            return {"success": False, "error": f"Insufficient stock to return"}

        item_total = item.quantity * item.selling_price
        total_amount += item_total

        # Reduce stock
        await db.execute(
            text("""
                UPDATE batches 
                SET quantity_remaining = quantity_remaining - :quantity,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :batch_id
            """),
            {"quantity": item.quantity, "batch_id": UUID(item.batch_id)}
        )

    # Create return record
    return_id = uuid.uuid4()
    await db.execute(
        text("""
            INSERT INTO supplier_returns (id, pharmacy_id, bill_id, supplier_id, 
                total_amount, reason, created_by, source)
            VALUES (:id, :pharmacy_id, :bill_id, :supplier_id, 
                :total_amount, :reason, :created_by, 'mobile')
        """),
        {
            "id": return_id,
            "pharmacy_id": UUID(pharmacy_id),
            "bill_id": UUID(request.bill_id),
            "supplier_id": bill[1],
            "total_amount": total_amount,
            "reason": request.reason,
            "created_by": UUID(user_id)
        }
    )

    await db.commit()

    return {
        "success": True,
        "data": {
            "return_id": str(return_id),
            "total_amount": total_amount,
            "message": "Return to supplier processed successfully"
        }
    }











# ============================================
# HEALTH CHECK
# ============================================
@app.get("/")
async def root():
    return {"message": "PharmaPro Cloud API", "version": "2.0.0", "status": "running"}


@app.get("/health")
async def health_check(db: AsyncSession = Depends(get_db)):
    try:
        await db.execute(text("SELECT 1"))
        return {"status": "healthy", "database": "connected"}
    except Exception as e:
        return {"status": "unhealthy", "database": str(e)}


# ============================================
# AUTHENTICATION ENDPOINT
# ============================================
@app.post("/api/auth/login", response_model=Token)
async def login(login_data: LoginRequest, db: AsyncSession = Depends(get_db)):
    auth_result = await authenticate_user(db, login_data.username, login_data.password)

    if not auth_result:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user, role_name = auth_result

    access_token = create_access_token(
        data={
            "sub": str(user.id),
            "username": user.username,
            "pharmacy_id": str(user.pharmacy_id),
            "role": role_name
        }
    )

    return Token(
        access_token=access_token,
        pharmacy_id=user.pharmacy_id,
        user_id=user.id,
        username=user.username,
        role=role_name
    )


# ============================================
# SYNC UPLOAD (Desktop pushes changes)
# ============================================
# ============================================
# SYNC UPLOAD (Desktop pushes changes)
# ============================================
@app.post("/api/sync/upload", response_model=SyncUploadResponse)
async def sync_upload(
        request: SyncUploadRequest,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """
    Desktop uploads its changes to cloud.
    Uses last-write-wins based on updated_at.
    Handles local INTEGER IDs via cloud_uuid mapping.
    Logs conflicts for audit purposes.
    """
    # Skip triggers during sync to avoid infinite loops
    await db.execute(text("SET app.skip_triggers = 'true'"))

    processed = 0
    conflicts = 0
    errors = []

    try:
        for change in request.changes:
            try:
                table_name = change.get("table_name")
                operation = change.get("operation")
                new_data = change.get("new_data", {})
                updated_at = datetime.fromisoformat(change.get("updated_at")) if change.get(
                    "updated_at") else datetime.utcnow()
                local_id = change.get("local_id")
                cloud_uuid_from_desktop = change.get("record_id")

                # Determine cloud UUID
                cloud_uuid = cloud_uuid_from_desktop

                # If no UUID provided, check mapping table
                if not cloud_uuid and local_id and table_name and request.pharmacy_id:
                    mapping_result = await db.execute(
                        text("""
                            SELECT cloud_uuid FROM id_mapping 
                            WHERE pharmacy_id = :pharmacy_id 
                                AND table_name = :table_name 
                                AND local_id = :local_id
                        """),
                        {
                            "pharmacy_id": request.pharmacy_id,
                            "table_name": table_name,
                            "local_id": local_id
                        }
                    )
                    existing = mapping_result.fetchone()
                    if existing:
                        cloud_uuid = str(existing[0])

                # If still no UUID, generate new one
                if not cloud_uuid:
                    cloud_uuid = str(uuid.uuid4())

                record_id = UUID(cloud_uuid)

                # Create or update mapping
                if local_id and table_name and request.pharmacy_id:
                    await db.execute(
                        text("""
                            INSERT INTO id_mapping (pharmacy_id, table_name, local_id, cloud_uuid)
                            VALUES (:pharmacy_id, :table_name, :local_id, :cloud_uuid)
                            ON CONFLICT (pharmacy_id, table_name, local_id) 
                            DO UPDATE SET cloud_uuid = :cloud_uuid, updated_at = CURRENT_TIMESTAMP
                        """),
                        {
                            "pharmacy_id": request.pharmacy_id,
                            "table_name": table_name,
                            "local_id": local_id,
                            "cloud_uuid": UUID(cloud_uuid)
                        }
                    )

                if operation == "DELETE":
                    check_query = text(f"""
                        SELECT id, updated_at, to_jsonb({table_name}) as record_data
                        FROM {table_name} 
                        WHERE id = :record_id AND pharmacy_id = :pharmacy_id AND is_deleted = FALSE
                    """)
                    existing_record = await db.execute(check_query, {
                        "record_id": record_id,
                        "pharmacy_id": request.pharmacy_id
                    })
                    existing_row = existing_record.first()

                    if existing_row:
                        query = text(f"""
                            UPDATE {table_name}
                            SET is_deleted = TRUE, 
                                updated_at = :updated_at,
                                sync_version = sync_version + 1
                            WHERE id = :record_id 
                                AND pharmacy_id = :pharmacy_id
                                AND (updated_at <= :updated_at OR updated_at IS NULL)
                        """)
                        result = await db.execute(query, {
                            "record_id": record_id,
                            "pharmacy_id": request.pharmacy_id,
                            "updated_at": updated_at
                        })
                        if result.rowcount > 0:
                            processed += 1
                        else:
                            conflicts += 1
                            # Log DELETE conflict
                            cloud_data = dict(existing_row[2]) if existing_row and len(existing_row) > 2 else None
                            await db.execute(
                                text("""
                                    INSERT INTO conflict_log (
                                        pharmacy_id, table_name, record_id, conflict_type,
                                        cloud_data, winner, cloud_updated_at, resolution_reason
                                    ) VALUES (
                                        :pharmacy_id, :table_name, :record_id, 'delete_conflict',
                                        :cloud_data::jsonb, 'cloud', :cloud_updated_at, :resolution_reason
                                    )
                                """),
                                {
                                    "pharmacy_id": request.pharmacy_id,
                                    "table_name": table_name,
                                    "record_id": record_id,
                                    "cloud_data": json.dumps(cloud_data) if cloud_data else None,
                                    "cloud_updated_at": existing_row[1] if existing_row and len(
                                        existing_row) > 1 else datetime.utcnow(),
                                    "resolution_reason": "Cloud record newer, desktop delete rejected"
                                }
                            )

                elif operation in ["INSERT", "UPDATE"]:
                    check_query = text(
                        f"SELECT id, updated_at, to_jsonb({table_name}) as record_data FROM {table_name} WHERE id = :record_id AND pharmacy_id = :pharmacy_id"
                    )
                    existing = await db.execute(check_query, {
                        "record_id": record_id,
                        "pharmacy_id": request.pharmacy_id
                    })
                    existing_row = existing.first()

                    existing_updated_at = existing_row[1] if existing_row else None
                    existing_data = dict(existing_row[2]) if existing_row and len(existing_row) > 2 else None

                    if existing_row and operation == "INSERT":
                        # Conflict: record exists but desktop sent INSERT
                        if updated_at > existing_updated_at:
                            await update_record(db, table_name, record_id, new_data, updated_at, request.pharmacy_id)
                            processed += 1
                            winner = "desktop"
                        else:
                            conflicts += 1
                            winner = "cloud"

                        # Log INSERT conflict
                        await db.execute(
                            text("""
                                INSERT INTO conflict_log (
                                    pharmacy_id, table_name, record_id, conflict_type,
                                    desktop_data, cloud_data, winner,
                                    desktop_updated_at, cloud_updated_at, resolution_reason
                                ) VALUES (
                                    :pharmacy_id, :table_name, :record_id, 'insert_conflict',
                                    :desktop_data::jsonb, :cloud_data::jsonb, :winner,
                                    :desktop_updated_at, :cloud_updated_at, :resolution_reason
                                )
                            """),
                            {
                                "pharmacy_id": request.pharmacy_id,
                                "table_name": table_name,
                                "record_id": record_id,
                                "desktop_data": json.dumps(new_data) if new_data else None,
                                "cloud_data": json.dumps(existing_data) if existing_data else None,
                                "winner": winner,
                                "desktop_updated_at": updated_at,
                                "cloud_updated_at": existing_updated_at,
                                "resolution_reason": "Last-write-wins based on updated_at timestamp"
                            }
                        )

                    elif existing_row and operation == "UPDATE":
                        if updated_at > existing_updated_at:
                            await update_record(db, table_name, record_id, new_data, updated_at, request.pharmacy_id)
                            processed += 1
                            winner = "desktop"
                        else:
                            conflicts += 1
                            winner = "cloud"

                        # Log UPDATE conflict
                        await db.execute(
                            text("""
                                INSERT INTO conflict_log (
                                    pharmacy_id, table_name, record_id, conflict_type,
                                    desktop_data, cloud_data, winner,
                                    desktop_updated_at, cloud_updated_at, resolution_reason
                                ) VALUES (
                                    :pharmacy_id, :table_name, :record_id, 'update_conflict',
                                    :desktop_data::jsonb, :cloud_data::jsonb, :winner,
                                    :desktop_updated_at, :cloud_updated_at, :resolution_reason
                                )
                            """),
                            {
                                "pharmacy_id": request.pharmacy_id,
                                "table_name": table_name,
                                "record_id": record_id,
                                "desktop_data": json.dumps(new_data) if new_data else None,
                                "cloud_data": json.dumps(existing_data) if existing_data else None,
                                "winner": winner,
                                "desktop_updated_at": updated_at,
                                "cloud_updated_at": existing_updated_at,
                                "resolution_reason": "Last-write-wins based on updated_at timestamp"
                            }
                        )

                    elif not existing_row and operation == "INSERT":
                        await insert_record(db, table_name, record_id, new_data, updated_at, request.pharmacy_id)
                        processed += 1

            except Exception as e:
                errors.append(f"Error processing {change.get('table_name')}/{change.get('record_id')}: {str(e)}")

        await db.commit()

        # Update sync state
        await db.execute(text("""
            UPDATE sync_state 
            SET last_sync_at = NOW(), 
                last_sync_version = :version,
                total_records_synced = total_records_synced + :processed,
                sync_status = 'completed'
            WHERE pharmacy_id = :pharmacy_id
        """), {
            "version": request.sync_version,
            "processed": processed,
            "pharmacy_id": request.pharmacy_id
        })

    finally:
        # Re-enable triggers
        await db.execute(text("SET app.skip_triggers = 'false'"))

    return SyncUploadResponse(
        success=True,
        records_processed=processed,
        conflicts_resolved=conflicts,
        new_sync_version=request.sync_version + 1,
        errors=errors
    )


# ============================================
# CATEGORIES - CRUD Operations
# ============================================

class CategoryCreate(BaseModel):
    name: str
    description: Optional[str] = None


class CategoryUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


@app.get("/api/categories")
async def get_categories(
        pharmacy_id: str,
        page: int = 1,
        limit: int = 50,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """Get all categories for a pharmacy"""
    offset = (page - 1) * limit

    query = text("""
        SELECT id, name, description, created_at, updated_at
        FROM categories
        WHERE pharmacy_id = :pharmacy_id AND is_deleted = FALSE
        ORDER BY name
        LIMIT :limit OFFSET :offset
    """)

    result = await db.execute(query, {
        "pharmacy_id": UUID(pharmacy_id),
        "limit": limit,
        "offset": offset
    })

    categories = []
    for row in result:
        categories.append({
            "id": str(row[0]),
            "name": row[1],
            "description": row[2],
            "created_at": row[3].isoformat() if row[3] else None,
            "updated_at": row[4].isoformat() if row[4] else None
        })

    # Get total count
    count_result = await db.execute(
        text("SELECT COUNT(*) FROM categories WHERE pharmacy_id = :pharmacy_id AND is_deleted = FALSE"),
        {"pharmacy_id": UUID(pharmacy_id)}
    )
    total = count_result.scalar() or 0

    return {
        "success": True,
        "data": categories,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "pages": (total + limit - 1) // limit
        }
    }


@app.post("/api/categories")
async def create_category(
        request: CategoryCreate,
        pharmacy_id: str,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """Create a new category"""
    # Check if category with same name exists
    existing = await db.execute(
        text("SELECT id FROM categories WHERE pharmacy_id = :pharmacy_id AND name = :name AND is_deleted = FALSE"),
        {"pharmacy_id": UUID(pharmacy_id), "name": request.name}
    )
    if existing.fetchone():
        return {"success": False, "error": "Category with this name already exists"}

    category_id = uuid.uuid4()
    await db.execute(
        text("""
            INSERT INTO categories (id, pharmacy_id, name, description, source, created_by)
            VALUES (:id, :pharmacy_id, :name, :description, 'mobile', :created_by)
        """),
        {
            "id": category_id,
            "pharmacy_id": UUID(pharmacy_id),
            "name": request.name,
            "description": request.description,
            "created_by": UUID(current_user.get("sub"))
        }
    )

    await db.commit()

    return {
        "success": True,
        "data": {"id": str(category_id)}
    }


@app.put("/api/categories/{category_id}")
async def update_category(
        category_id: str,
        request: CategoryUpdate,
        pharmacy_id: str,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """Update a category"""
    # Build dynamic update query
    updates = []
    params = {"id": UUID(category_id), "pharmacy_id": UUID(pharmacy_id)}

    if request.name:
        updates.append("name = :name")
        params["name"] = request.name
    if request.description is not None:
        updates.append("description = :description")
        params["description"] = request.description

    if not updates:
        return {"success": False, "error": "No fields to update"}

    updates.append("updated_at = CURRENT_TIMESTAMP")
    updates.append("sync_version = sync_version + 1")

    query = text(f"""
        UPDATE categories 
        SET {', '.join(updates)}
        WHERE id = :id AND pharmacy_id = :pharmacy_id AND is_deleted = FALSE
    """)

    result = await db.execute(query, params)
    await db.commit()

    if result.rowcount == 0:
        return {"success": False, "error": "Category not found"}

    return {"success": True, "message": "Category updated"}


@app.delete("/api/categories/{category_id}")
async def delete_category(
        category_id: str,
        pharmacy_id: str,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """Soft delete a category"""
    # Check if category has medicines
    medicines_result = await db.execute(
        text("SELECT COUNT(*) FROM medicines WHERE category_id = :category_id AND is_deleted = FALSE"),
        {"category_id": UUID(category_id)}
    )
    medicine_count = medicines_result.scalar() or 0

    if medicine_count > 0:
        return {"success": False, "error": f"Cannot delete category with {medicine_count} medicines"}

    await db.execute(
        text("""
            UPDATE categories 
            SET is_deleted = TRUE, updated_at = CURRENT_TIMESTAMP
            WHERE id = :id AND pharmacy_id = :pharmacy_id
        """),
        {"id": UUID(category_id), "pharmacy_id": UUID(pharmacy_id)}
    )

    await db.commit()

    return {"success": True, "message": "Category deleted"}






# ============================================
# SYNC DOWNLOAD (Desktop pulls changes)
# ============================================
@app.post("/api/sync/download", response_model=SyncDownloadResponse)
async def sync_download(
    request: SyncDownloadRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """
    Desktop pulls changes from cloud that happened since last sync.
    """
    # Get pending changes from change_log
    query = text("""
        SELECT table_name, record_id, operation, new_data, sync_version, changed_at
        FROM get_pending_changes(:pharmacy_id, :since_version)
        LIMIT :limit
    """)

    result = await db.execute(query, {
        "pharmacy_id": request.pharmacy_id,
        "since_version": request.since_version,
        "limit": request.limit
    })

    changes = []
    for row in result:
        changes.append({
            "table_name": row[0],
            "record_id": str(row[1]),
            "operation": row[2],
            "data": row[3],
            "sync_version": row[4],
            "changed_at": row[5].isoformat() if row[5] else None
        })

    # Get current max version
    version_query = text("SELECT COALESCE(MAX(sync_version), 0) FROM change_log WHERE pharmacy_id = :pharmacy_id")
    version_result = await db.execute(version_query, {"pharmacy_id": request.pharmacy_id})
    current_version = version_result.scalar() or 0

    return SyncDownloadResponse(
        success=True,
        changes=changes,
        current_version=current_version,
        has_more=len(changes) == request.limit
    )


# ============================================
# HELPER FUNCTIONS
# ============================================
async def update_record(db: AsyncSession, table_name: str, record_id: UUID, data: dict, updated_at: datetime):
    """Update record with last-write-wins"""
    # Remove id and pharmacy_id from data if present
    data.pop("id", None)
    data.pop("pharmacy_id", None)

    # Build SET clause
    set_clause = ", ".join([f"{key} = :{key}" for key in data.keys()])
    query = text(f"""
        UPDATE {table_name}
        SET {set_clause}, updated_at = :updated_at, sync_version = sync_version + 1
        WHERE id = :record_id AND (updated_at < :updated_at OR updated_at IS NULL)
    """)

    params = {**data, "record_id": record_id, "updated_at": updated_at}
    await db.execute(query, params)


async def insert_record(db: AsyncSession, table_name: str, record_id: UUID, data: dict,
                        updated_at: datetime, pharmacy_id: UUID):
    """Insert new record with proper column mapping"""

    # Remove fields that shouldn't be inserted
    data.pop("id", None)
    data.pop("pharmacy_id", None)
    data.pop("created_at", None)
    data.pop("customer_id", None)  # SQLite field, not in PostgreSQL
    data.pop("medicine_id", None)  # SQLite field, not in PostgreSQL

    # Extract cloud_uuid if present - we'll use it as the id
    cloud_uuid = data.pop("cloud_uuid", None)
    actual_id = UUID(cloud_uuid) if cloud_uuid else record_id

    # Base insert data
    insert_data = {
        "id": actual_id,
        "pharmacy_id": pharmacy_id,
        "updated_at": updated_at,
        "sync_version": 1,
        "source": "desktop"
    }

    # Map SQLite field names to PostgreSQL field names
    field_mapping = {
        'full_name': 'full_name',
        'phone': 'phone',
        'address': 'address',
        'balance': 'balance',
        'credit_limit': 'credit_limit',
        'total_purchases': 'total_purchases',
        'total_payments': 'total_payments',
        'last_payment_date': 'last_payment_date',
        'name': 'name',
        'brand': 'brand',
        'generic_name': 'generic_name',
        'strength': 'strength',
        'dosage_form': 'dosage_form',
        'barcode': 'barcode',
        'batch_number': 'batch_number',
        'expiry_date': 'expiry_date',
        'purchase_price': 'purchase_price',
        'selling_price': 'selling_price',
        'quantity_received': 'quantity_received',
        'quantity_remaining': 'quantity_remaining',
        'invoice_number': 'invoice_number',
        'total_amount': 'total_amount',
        'discount': 'discount',
        'net_amount': 'net_amount',
        'status': 'status',
        'is_credit': 'is_credit',
        'amount_paid': 'amount_paid',
        'balance_due': 'balance_due',
        'due_date': 'due_date',
        'created_by': 'created_by',
        # NEW MAPPINGS FOR CATEGORIES AND PACKAGING
        'category_id': 'category_id',
        'purchase_unit_id': 'purchase_unit_id',
        'pack_size': 'pack_size',
        'subunit_size': 'subunit_size',
        'smallest_unit_factor': 'smallest_unit_factor',
        'is_default': 'is_default',
        'unit_type_id': 'unit_type_id'
    }

    # Add mapped fields
    for key, value in data.items():
        if value is not None and value != "":
            pg_key = field_mapping.get(key, key)
            if pg_key in ['full_name', 'phone', 'address', 'name', 'brand', 'generic_name',
                          'strength', 'dosage_form', 'barcode', 'batch_number', 'invoice_number',
                          'status', 'balance', 'credit_limit', 'total_purchases', 'total_payments',
                          'purchase_price', 'selling_price', 'quantity_received', 'quantity_remaining',
                          'total_amount', 'discount', 'net_amount', 'amount_paid', 'balance_due',
                          'category_id', 'purchase_unit_id', 'pack_size', 'subunit_size',
                          'smallest_unit_factor', 'is_default', 'unit_type_id']:
                insert_data[pg_key] = value
            elif pg_key == 'expiry_date' or pg_key == 'last_payment_date' or pg_key == 'due_date':
                if isinstance(value, str):
                    insert_data[pg_key] = datetime.fromisoformat(value.replace('Z', '+00:00'))
                else:
                    insert_data[pg_key] = value
            elif pg_key == 'created_by':
                try:
                    insert_data[pg_key] = UUID(value) if value else None
                except:
                    pass

    # Build and execute insert for main table
    columns = list(insert_data.keys())
    placeholders = [f":{col}" for col in columns]

    query = text(f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES ({', '.join(placeholders)})")

    try:
        await db.execute(query, insert_data)

        # ========== NEW: Handle batch_units when inserting a batch ==========
        if table_name == 'batches':
            # Extract unit information from data (original data, not insert_data)
            unit_type_id = data.get('unit_type_id')
            pack_size = data.get('pack_size')
            subunit_size = data.get('subunit_size')
            smallest_unit_factor = data.get('smallest_unit_factor', 1)

            if unit_type_id:
                # Convert unit_type_id to UUID if it's a string
                try:
                    unit_type_uuid = UUID(unit_type_id) if isinstance(unit_type_id, str) else unit_type_id
                except:
                    # If it's not a UUID, try to find the system unit by name
                    unit_name = str(unit_type_id)
                    unit_result = await db.execute(
                        text(
                            "SELECT id FROM unit_types WHERE name = :name AND (is_system = TRUE OR pharmacy_id = :pharmacy_id)"),
                        {"name": unit_name, "pharmacy_id": pharmacy_id}
                    )
                    unit_row = unit_result.fetchone()
                    if unit_row:
                        unit_type_uuid = unit_row[0]
                    else:
                        # Create new unit type for this pharmacy
                        unit_type_uuid = uuid.uuid4()
                        await db.execute(
                            text("""
                                INSERT INTO unit_types (id, pharmacy_id, name, is_smallest_unit, is_system, source)
                                VALUES (:id, :pharmacy_id, :name, FALSE, FALSE, 'desktop')
                            """),
                            {"id": unit_type_uuid, "pharmacy_id": pharmacy_id, "name": unit_name}
                        )

                # Insert batch_units record
                batch_unit_id = uuid.uuid4()
                await db.execute(
                    text("""
                        INSERT INTO batch_units (
                            id, pharmacy_id, batch_id, unit_type_id, pack_size, 
                            subunit_size, smallest_unit_factor, purchase_price_per_unit, 
                            selling_price_per_unit, source
                        )
                        VALUES (
                            :id, :pharmacy_id, :batch_id, :unit_type_id, :pack_size,
                            :subunit_size, :smallest_unit_factor, :purchase_price, 
                            :selling_price, 'desktop'
                        )
                    """),
                    {
                        "id": batch_unit_id,
                        "pharmacy_id": pharmacy_id,
                        "batch_id": actual_id,
                        "unit_type_id": unit_type_uuid,
                        "pack_size": pack_size if pack_size else None,
                        "subunit_size": subunit_size if subunit_size else None,
                        "smallest_unit_factor": smallest_unit_factor,
                        "purchase_price": data.get('purchase_price', 0),
                        "selling_price": data.get('selling_price', 0)
                    }
                )

        # ========== NEW: Handle medicine_packaging_templates ==========
        elif table_name == 'medicine_packaging_templates':
            # Already inserted, nothing extra needed
            pass

    except Exception as e:
        print(f"Insert error for {table_name}: {e}")
        print(f"Columns: {columns}")
        print(f"Data keys: {list(insert_data.keys())}")
        raise


## Unit Types mapping endpoint
@app.post("/api/unit-types/map")
async def map_unit_type(
        pharmacy_id: str,
        desktop_id: int,
        unit_name: str,
        db: AsyncSession = Depends(get_db),
        current_user: dict = Depends(get_current_user)
):
    """Map desktop unit type ID to cloud unit type UUID"""
    # First try to find system unit by name
    result = await db.execute(
        text("SELECT id FROM unit_types WHERE name = :name AND (is_system = TRUE OR pharmacy_id = :pharmacy_id)"),
        {"name": unit_name, "pharmacy_id": UUID(pharmacy_id)}
    )
    cloud_unit = result.fetchone()

    if not cloud_unit:
        # Create new unit type for this pharmacy
        unit_id = uuid.uuid4()
        await db.execute(
            text("""
                INSERT INTO unit_types (id, pharmacy_id, name, is_smallest_unit, is_system)
                VALUES (:id, :pharmacy_id, :name, FALSE, FALSE)
            """),
            {"id": unit_id, "pharmacy_id": UUID(pharmacy_id), "name": unit_name}
        )
        cloud_unit_id = unit_id
    else:
        cloud_unit_id = cloud_unit[0]

    # Create or update mapping
    await db.execute(
        text("""
            INSERT INTO unit_type_mapping (pharmacy_id, desktop_unit_type_id, cloud_unit_type_id)
            VALUES (:pharmacy_id, :desktop_id, :cloud_id)
            ON CONFLICT (pharmacy_id, desktop_unit_type_id)
            DO UPDATE SET cloud_unit_type_id = :cloud_id
        """),
        {
            "pharmacy_id": UUID(pharmacy_id),
            "desktop_id": desktop_id,
            "cloud_id": cloud_unit_id
        }
    )

    await db.commit()

    return {
        "success": True,
        "data": {
            "desktop_unit_type_id": desktop_id,
            "cloud_unit_type_id": str(cloud_unit_id)
        }
    }

@app.get("/api/debug/user-check")
async def debug_user_check(username: str, db: AsyncSession = Depends(get_db)):
    try:
        # Check if user exists
        result = await db.execute(
            text("SELECT username, password_hash, is_active, pharmacy_id FROM users WHERE username = :username"),
            {"username": username}
        )
        user = result.fetchone()

        if not user:
            return {"exists": False, "message": f"User '{username}' not found"}

        return {
            "exists": True,
            "username": user[0],
            "hash_prefix": user[1][:30] if user[1] else None,
            "is_active": user[2],
            "pharmacy_id": str(user[3])
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/debug/verify-password")
async def debug_verify_password(username: str, password: str, db: AsyncSession = Depends(get_db)):
    try:
        # Get user
        result = await db.execute(
            text("SELECT username, password_hash, is_active FROM users WHERE username = :username"),
            {"username": username}
        )
        user = result.fetchone()

        if not user:
            return {"exists": False, "message": f"User '{username}' not found"}

        # Try to verify password
        from passlib.context import CryptContext
        pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

        is_valid = pwd_context.verify(password, user[1])

        return {
            "exists": True,
            "username": user[0],
            "hash_prefix": user[1][:30] if user[1] else None,
            "is_active": user[2],
            "password_valid": is_valid
        }
    except Exception as e:
        return {"error": str(e)}
# ============================================
# RUN SERVER
# ============================================
# Startup event
@app.on_event("startup")
async def startup_event():
    """Initialize system on startup"""
    from app.database import AsyncSessionLocal
    from app.auth import get_password_hash

    async with AsyncSessionLocal() as db:
        # FIRST: Check if roles exist, if not create them
        roles_check = await db.execute(
            text("SELECT COUNT(*) FROM roles")
        )
        role_count = roles_check.scalar() or 0

        if role_count == 0:
            # Seed default roles
            await db.execute(
                text("""
                    INSERT INTO roles (id, name, description, permissions) VALUES
                    (gen_random_uuid(), 'Administrator', 'Full system access', '{"all": true}'::jsonb),
                    (gen_random_uuid(), 'Pharmacist', 'Can manage medicines and sales', '{"medicines": ["view","add","edit"], "sales": ["pos","view"]}'::jsonb),
                    (gen_random_uuid(), 'Cashier', 'Processes sales only', '{"sales": ["pos","view"], "customers": ["view","add"]}'::jsonb),
                    (gen_random_uuid(), 'Manager', 'Manager with limited deletion', '{"medicines": ["view","add","edit"], "reports": ["view","export"]}'::jsonb),
                    (gen_random_uuid(), 'Staff', 'Basic read-only access', '{"medicines": ["view"], "sales": ["view"]}'::jsonb)
                """)
            )
            await db.commit()
            print("✅ Default roles created")

        # Check if pharmacy exists
        pharmacy_check = await db.execute(
            text("SELECT COUNT(*) FROM pharmacies")
        )
        pharmacy_count = pharmacy_check.scalar() or 0

        if pharmacy_count == 0:
            await db.execute(
                text("""
                    INSERT INTO pharmacies (id, name, hwid, subscription_type, is_active)
                    VALUES (gen_random_uuid(), 'Default Pharmacy', 'DEFAULT-HWID-001', 'trial', TRUE)
                """)
            )
            await db.commit()
            print("✅ Default pharmacy created")

        # NOW create sync system user if not exists
        sync_user_check = await db.execute(
            text("SELECT id FROM users WHERE username = 'sync_system'")
        )
        if not sync_user_check.fetchone():
            admin_role = await db.execute(
                text("SELECT id FROM roles WHERE name = 'Administrator'")
            )
            role_row = admin_role.fetchone()
            if role_row:
                role_id = role_row[0]
                pharmacy = await db.execute(text("SELECT id FROM pharmacies LIMIT 1"))
                pharmacy_row = pharmacy.fetchone()
                if pharmacy_row:
                    pharmacy_id = pharmacy_row[0]

                    await db.execute(
                        text("""
                            INSERT INTO users (id, pharmacy_id, role_id, fullname, username, password_hash, email, is_active, is_verified)
                            VALUES (gen_random_uuid(), :pharmacy_id, :role_id, 'Sync System', 'sync_system', :password_hash, 'sync@system.local', TRUE, TRUE)
                        """),
                        {
                            "pharmacy_id": pharmacy_id,
                            "role_id": role_id,
                            "password_hash": get_password_hash('SyncSystem2024!')
                        }
                    )
                    await db.commit()
                    print("✅ Sync system user created")

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, reload=settings.DEBUG)