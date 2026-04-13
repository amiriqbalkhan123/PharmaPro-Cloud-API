BEGIN;

-- =========================================================
-- EXTENSIONS
-- =========================================================
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- =========================================================
-- CORE FUNCTIONS
-- =========================================================
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


CREATE OR REPLACE FUNCTION log_table_changes()
RETURNS TRIGGER AS $$
DECLARE
    old_data_json JSONB;
    new_data_json JSONB;
    changed_fields_arr TEXT[];
    current_sync_ver INTEGER;
    skip_triggers BOOLEAN := FALSE;

    v_pharmacy_id UUID;
    v_record_id UUID;
    v_changed_by UUID;
    ref_uuid UUID;
BEGIN
    BEGIN
        skip_triggers := COALESCE(current_setting('app.skip_triggers', TRUE)::boolean, FALSE);
    EXCEPTION WHEN OTHERS THEN
        skip_triggers := FALSE;
    END;

    IF skip_triggers THEN
        IF TG_OP IN ('INSERT', 'UPDATE') THEN
            RETURN NEW;
        ELSE
            RETURN OLD;
        END IF;
    END IF;

    IF TG_OP = 'DELETE' THEN
        old_data_json := to_jsonb(OLD);
        new_data_json := NULL;
        changed_fields_arr := ARRAY['*'];
        v_record_id := OLD.id;
        v_changed_by := NULLIF(old_data_json->>'created_by', '')::UUID;
        v_pharmacy_id := NULLIF(old_data_json->>'pharmacy_id', '')::UUID;
        current_sync_ver := COALESCE(OLD.sync_version, 0) + 1;
    ELSIF TG_OP = 'INSERT' THEN
        old_data_json := NULL;
        new_data_json := to_jsonb(NEW);
        changed_fields_arr := ARRAY['*'];
        v_record_id := NEW.id;
        v_changed_by := NULLIF(new_data_json->>'created_by', '')::UUID;
        v_pharmacy_id := NULLIF(new_data_json->>'pharmacy_id', '')::UUID;
        NEW.sync_version := 1;
        current_sync_ver := 1;
    ELSIF TG_OP = 'UPDATE' THEN
        old_data_json := to_jsonb(OLD);
        new_data_json := to_jsonb(NEW);

        SELECT array_agg(key)
        INTO changed_fields_arr
        FROM jsonb_each(to_jsonb(NEW))
        WHERE jsonb_each.value IS DISTINCT FROM (to_jsonb(OLD)->key);

        IF changed_fields_arr IS NULL THEN
            changed_fields_arr := ARRAY[]::TEXT[];
        END IF;

        v_record_id := NEW.id;
        v_changed_by := NULLIF(new_data_json->>'created_by', '')::UUID;
        v_pharmacy_id := NULLIF(new_data_json->>'pharmacy_id', '')::UUID;

        current_sync_ver := COALESCE(OLD.sync_version, 0) + 1;
        NEW.sync_version := current_sync_ver;
    END IF;

    -- Derive pharmacy_id for detail tables that don't store it directly
    IF v_pharmacy_id IS NULL THEN
        IF TG_TABLE_NAME = 'invoice_details' THEN
            ref_uuid := COALESCE(NULLIF((COALESCE(new_data_json, old_data_json)->>'invoice_id'), '')::UUID, NULL);
            IF ref_uuid IS NOT NULL THEN
                SELECT pharmacy_id INTO v_pharmacy_id FROM invoices WHERE id = ref_uuid;
            END IF;

        ELSIF TG_TABLE_NAME = 'bill_details' THEN
            ref_uuid := COALESCE(NULLIF((COALESCE(new_data_json, old_data_json)->>'bill_id'), '')::UUID, NULL);
            IF ref_uuid IS NOT NULL THEN
                SELECT pharmacy_id INTO v_pharmacy_id FROM bills WHERE id = ref_uuid;
            END IF;

        ELSIF TG_TABLE_NAME = 'customer_return_details' THEN
            ref_uuid := COALESCE(NULLIF((COALESCE(new_data_json, old_data_json)->>'return_id'), '')::UUID, NULL);
            IF ref_uuid IS NOT NULL THEN
                SELECT pharmacy_id INTO v_pharmacy_id FROM customer_returns WHERE id = ref_uuid;
            END IF;

        ELSIF TG_TABLE_NAME = 'supplier_return_details' THEN
            ref_uuid := COALESCE(NULLIF((COALESCE(new_data_json, old_data_json)->>'return_id'), '')::UUID, NULL);
            IF ref_uuid IS NOT NULL THEN
                SELECT pharmacy_id INTO v_pharmacy_id FROM supplier_returns WHERE id = ref_uuid;
            END IF;
        END IF;
    END IF;

    -- Skip change log insert if no pharmacy context is available
    -- This allows global/system unit_types to exist without polluting change_log
    IF v_pharmacy_id IS NOT NULL THEN
        INSERT INTO change_log (
            table_name,
            record_id,
            pharmacy_id,
            operation,
            old_data,
            new_data,
            changed_fields,
            sync_version,
            synced_to_cloud,
            changed_by,
            source
        )
        VALUES (
            TG_TABLE_NAME,
            v_record_id,
            v_pharmacy_id,
            TG_OP,
            old_data_json,
            new_data_json,
            changed_fields_arr,
            current_sync_ver,
            FALSE,
            v_changed_by,
            COALESCE(COALESCE(new_data_json, old_data_json)->>'source', 'desktop')
        );
    END IF;

    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        RETURN NEW;
    ELSE
        RETURN OLD;
    END IF;
END;
$$ LANGUAGE plpgsql;


CREATE OR REPLACE FUNCTION get_pending_changes(
    p_pharmacy_id UUID,
    p_since_version BIGINT
)
RETURNS TABLE(
    id BIGINT,
    table_name VARCHAR,
    record_id UUID,
    operation VARCHAR,
    new_data JSONB,
    sync_version INTEGER,
    changed_at TIMESTAMP
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        cl.id,
        cl.table_name,
        cl.record_id,
        cl.operation,
        cl.new_data,
        cl.sync_version,
        cl.changed_at
    FROM change_log cl
    WHERE cl.pharmacy_id = p_pharmacy_id
      AND cl.sync_version > p_since_version
    ORDER BY cl.sync_version ASC, cl.id ASC;
END;
$$ LANGUAGE plpgsql;


CREATE OR REPLACE FUNCTION mark_changes_synced(p_change_ids BIGINT[])
RETURNS VOID AS $$
BEGIN
    UPDATE change_log
    SET synced_to_cloud = TRUE,
        synced_at = CURRENT_TIMESTAMP
    WHERE id = ANY(p_change_ids);
END;
$$ LANGUAGE plpgsql;


-- =========================================================
-- TABLES
-- =========================================================

-- 1. PHARMACIES
CREATE TABLE pharmacies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(100) NOT NULL,
    hwid VARCHAR(64) UNIQUE NOT NULL,
    owner_name VARCHAR(100),
    phone VARCHAR(20),
    email VARCHAR(100),
    address TEXT,
    city VARCHAR(50),
    province VARCHAR(50),
    subscription_type VARCHAR(20) DEFAULT 'trial',
    subscription_expiry TIMESTAMP,
    is_active BOOLEAN DEFAULT TRUE,
    last_sync_at TIMESTAMP,
    sync_version BIGINT DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. ROLES
CREATE TABLE roles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(50) NOT NULL UNIQUE,
    description TEXT,
    permissions JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 3. USERS
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pharmacy_id UUID NOT NULL REFERENCES pharmacies(id) ON DELETE CASCADE,
    role_id UUID NOT NULL REFERENCES roles(id),
    fullname VARCHAR(100) NOT NULL,
    username VARCHAR(50) NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    email VARCHAR(100) UNIQUE,
    phone VARCHAR(20),
    is_active BOOLEAN DEFAULT TRUE,
    is_verified BOOLEAN DEFAULT FALSE,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted BOOLEAN DEFAULT FALSE,
    sync_version INTEGER DEFAULT 1,
    source VARCHAR(20) DEFAULT 'desktop',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_by UUID REFERENCES users(id)
);

-- 4. CATEGORIES
CREATE TABLE categories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pharmacy_id UUID NOT NULL REFERENCES pharmacies(id) ON DELETE CASCADE,
    name VARCHAR(50) NOT NULL,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted BOOLEAN DEFAULT FALSE,
    sync_version INTEGER DEFAULT 1,
    source VARCHAR(20) DEFAULT 'desktop',
    created_by UUID REFERENCES users(id),
    UNIQUE (pharmacy_id, name)
);

-- 5. CUSTOMERS
CREATE TABLE customers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pharmacy_id UUID NOT NULL REFERENCES pharmacies(id) ON DELETE CASCADE,
    full_name VARCHAR(100) NOT NULL,
    phone VARCHAR(20),
    address TEXT,
    balance DECIMAL(10,2) DEFAULT 0,
    credit_limit DECIMAL(10,2) DEFAULT 0,
    total_purchases DECIMAL(10,2) DEFAULT 0,
    total_payments DECIMAL(10,2) DEFAULT 0,
    last_payment_date TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted BOOLEAN DEFAULT FALSE,
    sync_version INTEGER DEFAULT 1,
    source VARCHAR(20) DEFAULT 'desktop',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_by UUID REFERENCES users(id)
);

-- 6. SUPPLIERS
CREATE TABLE suppliers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pharmacy_id UUID NOT NULL REFERENCES pharmacies(id) ON DELETE CASCADE,
    name VARCHAR(100) NOT NULL,
    contact_person VARCHAR(100),
    phone VARCHAR(20),
    email VARCHAR(100),
    address TEXT,
    balance DECIMAL(10,2) DEFAULT 0,
    total_purchases DECIMAL(10,2) DEFAULT 0,
    total_payments DECIMAL(10,2) DEFAULT 0,
    last_payment_date TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted BOOLEAN DEFAULT FALSE,
    sync_version INTEGER DEFAULT 1,
    source VARCHAR(20) DEFAULT 'desktop',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_by UUID REFERENCES users(id)
);

-- 7. UNIT TYPES
CREATE TABLE unit_types (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pharmacy_id UUID REFERENCES pharmacies(id) ON DELETE CASCADE,
    name VARCHAR(50) NOT NULL,
    is_smallest_unit BOOLEAN DEFAULT FALSE,
    is_system BOOLEAN DEFAULT FALSE,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted BOOLEAN DEFAULT FALSE,
    sync_version INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_by UUID REFERENCES users(id),
    UNIQUE (pharmacy_id, name)
);

-- 8. MEDICINES
CREATE TABLE medicines (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pharmacy_id UUID NOT NULL REFERENCES pharmacies(id) ON DELETE CASCADE,
    category_id UUID REFERENCES categories(id),
    name VARCHAR(100) NOT NULL,
    generic_name VARCHAR(100),
    brand VARCHAR(100),
    dosage_form VARCHAR(50),
    strength VARCHAR(50),
    barcode VARCHAR(50),
    smallest_unit VARCHAR(20),
    has_strips BOOLEAN DEFAULT FALSE,
    units_per_strip INTEGER,
    has_packs BOOLEAN DEFAULT FALSE,
    units_per_pack INTEGER,
    strips_per_pack INTEGER,
    has_bottles BOOLEAN DEFAULT FALSE,
    units_per_bottle INTEGER,
    low_stock_threshold_packs INTEGER DEFAULT 5,
    low_stock_threshold_strips INTEGER DEFAULT 10,
    low_stock_threshold_tablets INTEGER DEFAULT 50,
    low_stock_threshold_bottles INTEGER DEFAULT 2,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted BOOLEAN DEFAULT FALSE,
    sync_version INTEGER DEFAULT 1,
    source VARCHAR(20) DEFAULT 'desktop',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_by UUID REFERENCES users(id)
);

-- 9. BATCHES
CREATE TABLE batches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pharmacy_id UUID NOT NULL REFERENCES pharmacies(id) ON DELETE CASCADE,
    medicine_id UUID NOT NULL REFERENCES medicines(id),
    batch_number VARCHAR(50) NOT NULL,
    expiry_date DATE,
    purchase_price DECIMAL(10,2) NOT NULL,
    selling_price DECIMAL(10,2) NOT NULL,
    quantity_received INTEGER NOT NULL,
    quantity_remaining INTEGER NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted BOOLEAN DEFAULT FALSE,
    sync_version INTEGER DEFAULT 1,
    source VARCHAR(20) DEFAULT 'desktop',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_by UUID REFERENCES users(id),
    UNIQUE(pharmacy_id, medicine_id, batch_number)
);

-- 10. BATCH UNITS
CREATE TABLE batch_units (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pharmacy_id UUID NOT NULL REFERENCES pharmacies(id) ON DELETE CASCADE,
    batch_id UUID NOT NULL UNIQUE REFERENCES batches(id) ON DELETE CASCADE,
    unit_type_id UUID NOT NULL REFERENCES unit_types(id),
    pack_size INTEGER,
    subunit_size INTEGER,
    smallest_unit_factor INTEGER NOT NULL DEFAULT 1,
    purchase_price_per_unit DECIMAL(10,2) NOT NULL,
    selling_price_per_unit DECIMAL(10,2) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted BOOLEAN DEFAULT FALSE,
    sync_version INTEGER DEFAULT 1,
    source VARCHAR(20) DEFAULT 'desktop',
    created_by UUID REFERENCES users(id)
);

-- 11. MEDICINE PACKAGING TEMPLATES
CREATE TABLE medicine_packaging_templates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pharmacy_id UUID NOT NULL REFERENCES pharmacies(id) ON DELETE CASCADE,
    medicine_id UUID NOT NULL REFERENCES medicines(id) ON DELETE CASCADE,
    purchase_unit_id UUID NOT NULL REFERENCES unit_types(id),
    pack_size INTEGER,
    subunit_size INTEGER,
    smallest_unit_factor INTEGER NOT NULL DEFAULT 1,
    is_default BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted BOOLEAN DEFAULT FALSE,
    sync_version INTEGER DEFAULT 1,
    source VARCHAR(20) DEFAULT 'desktop',
    created_by UUID REFERENCES users(id)
);

-- 12. INVOICES
CREATE TABLE invoices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pharmacy_id UUID NOT NULL REFERENCES pharmacies(id) ON DELETE CASCADE,
    customer_id UUID REFERENCES customers(id),
    invoice_number VARCHAR(50) NOT NULL UNIQUE,
    invoice_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    total_amount DECIMAL(10,2) DEFAULT 0,
    discount DECIMAL(10,2) DEFAULT 0,
    net_amount DECIMAL(10,2) DEFAULT 0,
    status VARCHAR(20) DEFAULT 'Paid',
    is_credit BOOLEAN DEFAULT FALSE,
    amount_paid DECIMAL(10,2) DEFAULT 0,
    balance_due DECIMAL(10,2) DEFAULT 0,
    due_date DATE,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted BOOLEAN DEFAULT FALSE,
    sync_version INTEGER DEFAULT 1,
    source VARCHAR(20) DEFAULT 'desktop',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_by UUID REFERENCES users(id)
);

-- 13. INVOICE DETAILS
CREATE TABLE invoice_details (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id UUID NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    batch_id UUID NOT NULL REFERENCES batches(id),
    unit_type_id UUID REFERENCES unit_types(id),
    unit_quantity INTEGER DEFAULT 0,
    quantity INTEGER NOT NULL,
    unit_price DECIMAL(10,2) NOT NULL,
    discount DECIMAL(10,2) DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted BOOLEAN DEFAULT FALSE,
    sync_version INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 14. BILLS
CREATE TABLE bills (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pharmacy_id UUID NOT NULL REFERENCES pharmacies(id) ON DELETE CASCADE,
    supplier_id UUID NOT NULL REFERENCES suppliers(id),
    bill_number VARCHAR(50) NOT NULL,
    bill_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    total_amount DECIMAL(10,2) DEFAULT 0,
    discount DECIMAL(10,2) DEFAULT 0,
    net_amount DECIMAL(10,2) DEFAULT 0,
    status VARCHAR(20) DEFAULT 'Paid',
    is_credit BOOLEAN DEFAULT FALSE,
    amount_paid DECIMAL(10,2) DEFAULT 0,
    balance_due DECIMAL(10,2) DEFAULT 0,
    due_date DATE,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted BOOLEAN DEFAULT FALSE,
    sync_version INTEGER DEFAULT 1,
    source VARCHAR(20) DEFAULT 'desktop',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_by UUID REFERENCES users(id)
);

-- 15. BILL DETAILS
CREATE TABLE bill_details (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    bill_id UUID NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    medicine_id UUID NOT NULL REFERENCES medicines(id),
    batch_number VARCHAR(50) NOT NULL,
    expiry_date DATE,
    quantity INTEGER NOT NULL,
    purchase_price DECIMAL(10,2) NOT NULL,
    selling_price DECIMAL(10,2) NOT NULL,
    discount DECIMAL(10,2) DEFAULT 0,
    unit_type_id UUID REFERENCES unit_types(id),
    pack_size INTEGER,
    subunit_size INTEGER,
    smallest_unit_factor INTEGER DEFAULT 1,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted BOOLEAN DEFAULT FALSE,
    sync_version INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 16. CUSTOMER RETURNS
CREATE TABLE customer_returns (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pharmacy_id UUID NOT NULL REFERENCES pharmacies(id) ON DELETE CASCADE,
    invoice_id UUID NOT NULL REFERENCES invoices(id),
    customer_id UUID NOT NULL REFERENCES customers(id),
    return_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    total_amount DECIMAL(10,2) NOT NULL,
    reason TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted BOOLEAN DEFAULT FALSE,
    sync_version INTEGER DEFAULT 1,
    source VARCHAR(20) DEFAULT 'desktop',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_by UUID REFERENCES users(id)
);

-- 17. CUSTOMER RETURN DETAILS
CREATE TABLE customer_return_details (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    return_id UUID NOT NULL REFERENCES customer_returns(id) ON DELETE CASCADE,
    batch_id UUID NOT NULL REFERENCES batches(id),
    quantity INTEGER NOT NULL,
    unit_price DECIMAL(10,2) NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted BOOLEAN DEFAULT FALSE,
    sync_version INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 18. SUPPLIER RETURNS
CREATE TABLE supplier_returns (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pharmacy_id UUID NOT NULL REFERENCES pharmacies(id) ON DELETE CASCADE,
    bill_id UUID NOT NULL REFERENCES bills(id),
    supplier_id UUID NOT NULL REFERENCES suppliers(id),
    return_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    total_amount DECIMAL(10,2) NOT NULL,
    reason TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted BOOLEAN DEFAULT FALSE,
    sync_version INTEGER DEFAULT 1,
    source VARCHAR(20) DEFAULT 'desktop',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_by UUID REFERENCES users(id)
);

-- 19. SUPPLIER RETURN DETAILS
CREATE TABLE supplier_return_details (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    return_id UUID NOT NULL REFERENCES supplier_returns(id) ON DELETE CASCADE,
    batch_id UUID NOT NULL REFERENCES batches(id),
    quantity INTEGER NOT NULL,
    unit_price DECIMAL(10,2) NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted BOOLEAN DEFAULT FALSE,
    sync_version INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 20. PAYMENTS
CREATE TABLE payments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pharmacy_id UUID NOT NULL REFERENCES pharmacies(id) ON DELETE CASCADE,
    party_type VARCHAR(20) NOT NULL,
    party_id UUID NOT NULL,
    reference_type VARCHAR(20),
    reference_id UUID,
    amount DECIMAL(10,2) NOT NULL,
    method VARCHAR(20) NOT NULL,
    payment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notes TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted BOOLEAN DEFAULT FALSE,
    sync_version INTEGER DEFAULT 1,
    source VARCHAR(20) DEFAULT 'desktop',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_by UUID REFERENCES users(id)
);

-- 21. INVENTORY LOGS
CREATE TABLE inventory_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pharmacy_id UUID NOT NULL REFERENCES pharmacies(id) ON DELETE CASCADE,
    medicine_id UUID NOT NULL REFERENCES medicines(id),
    batch_id UUID NOT NULL REFERENCES batches(id),
    reference_type VARCHAR(20) NOT NULL,
    reference_id UUID NOT NULL,
    change_type VARCHAR(20) NOT NULL,
    before_quantity INTEGER NOT NULL,
    after_quantity INTEGER NOT NULL,
    before_smallest_units INTEGER,
    after_smallest_units INTEGER,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted BOOLEAN DEFAULT FALSE,
    sync_version INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_by UUID REFERENCES users(id)
);

-- 22. ACTIVITY LOGS
CREATE TABLE activity_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pharmacy_id UUID NOT NULL REFERENCES pharmacies(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id),
    action VARCHAR(100) NOT NULL,
    module VARCHAR(50) NOT NULL,
    description TEXT,
    ip_address INET,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 23. CHANGE LOG
CREATE TABLE change_log (
    id BIGSERIAL PRIMARY KEY,
    table_name VARCHAR(50) NOT NULL,
    record_id UUID NOT NULL,
    pharmacy_id UUID NOT NULL REFERENCES pharmacies(id) ON DELETE CASCADE,
    operation VARCHAR(10) NOT NULL,
    old_data JSONB,
    new_data JSONB,
    changed_fields TEXT[],
    sync_version INTEGER NOT NULL,
    synced_to_cloud BOOLEAN DEFAULT FALSE,
    synced_at TIMESTAMP,
    changed_by UUID REFERENCES users(id),
    changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    source VARCHAR(20) DEFAULT 'desktop'
);

-- 24. SYNC STATE
CREATE TABLE sync_state (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pharmacy_id UUID NOT NULL UNIQUE REFERENCES pharmacies(id) ON DELETE CASCADE,
    last_sync_at TIMESTAMP,
    last_sync_version BIGINT DEFAULT 0,
    last_sync_record_id BIGINT,
    current_sync_id UUID,
    sync_started_at TIMESTAMP,
    sync_status VARCHAR(20) DEFAULT 'idle',
    total_records_synced INTEGER DEFAULT 0,
    total_errors INTEGER DEFAULT 0,
    last_error TEXT,
    last_checkpoint_table VARCHAR(50),
    last_checkpoint_id UUID,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 25. ID MAPPING
CREATE TABLE id_mapping (
    id BIGSERIAL PRIMARY KEY,
    pharmacy_id UUID NOT NULL REFERENCES pharmacies(id) ON DELETE CASCADE,
    table_name VARCHAR(50) NOT NULL,
    local_id INTEGER NOT NULL,
    cloud_uuid UUID NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (pharmacy_id, table_name, local_id),
    UNIQUE (pharmacy_id, table_name, cloud_uuid)
);

-- 26. UNIT TYPE MAPPING
CREATE TABLE unit_type_mapping (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pharmacy_id UUID NOT NULL REFERENCES pharmacies(id) ON DELETE CASCADE,
    desktop_unit_type_id INTEGER NOT NULL,
    cloud_unit_type_id UUID NOT NULL REFERENCES unit_types(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(pharmacy_id, desktop_unit_type_id)
);

-- 27. CONFLICT LOG
CREATE TABLE conflict_log (
    id BIGSERIAL PRIMARY KEY,
    pharmacy_id UUID NOT NULL REFERENCES pharmacies(id) ON DELETE CASCADE,
    table_name VARCHAR(50) NOT NULL,
    record_id UUID NOT NULL,
    conflict_type VARCHAR(20) NOT NULL,
    desktop_data JSONB,
    cloud_data JSONB,
    winner VARCHAR(20) NOT NULL,
    desktop_updated_at TIMESTAMP,
    cloud_updated_at TIMESTAMP,
    resolution_reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- =========================================================
-- INDEXES
-- =========================================================
CREATE INDEX idx_categories_pharmacy ON categories(pharmacy_id);
CREATE INDEX idx_categories_name ON categories(name);

CREATE INDEX idx_customers_pharmacy ON customers(pharmacy_id);
CREATE INDEX idx_customers_name ON customers(full_name);
CREATE INDEX idx_customers_updated ON customers(updated_at);

CREATE INDEX idx_suppliers_pharmacy ON suppliers(pharmacy_id);
CREATE INDEX idx_suppliers_name ON suppliers(name);

CREATE INDEX idx_unit_types_system ON unit_types(is_system);

CREATE INDEX idx_medicines_pharmacy ON medicines(pharmacy_id);
CREATE INDEX idx_medicines_name ON medicines(name);
CREATE INDEX idx_medicines_barcode ON medicines(barcode);
CREATE INDEX idx_medicines_category ON medicines(category_id);
CREATE INDEX idx_medicines_updated ON medicines(updated_at);

CREATE INDEX idx_batches_pharmacy ON batches(pharmacy_id);
CREATE INDEX idx_batches_medicine ON batches(medicine_id);
CREATE INDEX idx_batches_expiry ON batches(expiry_date);
CREATE INDEX idx_batches_updated ON batches(updated_at);

CREATE INDEX idx_batch_units_batch ON batch_units(batch_id);
CREATE INDEX idx_batch_units_unit_type ON batch_units(unit_type_id);
CREATE INDEX idx_batch_units_pharmacy ON batch_units(pharmacy_id);

CREATE INDEX idx_med_template_medicine ON medicine_packaging_templates(medicine_id);
CREATE INDEX idx_med_template_unit ON medicine_packaging_templates(purchase_unit_id);
CREATE INDEX idx_med_template_pharmacy ON medicine_packaging_templates(pharmacy_id);

CREATE INDEX idx_invoices_pharmacy ON invoices(pharmacy_id);
CREATE INDEX idx_invoices_customer ON invoices(customer_id);
CREATE INDEX idx_invoices_number ON invoices(invoice_number);
CREATE INDEX idx_invoices_date ON invoices(invoice_date);
CREATE INDEX idx_invoices_updated ON invoices(updated_at);

CREATE INDEX idx_invoicedetails_invoice ON invoice_details(invoice_id);
CREATE INDEX idx_invoicedetails_batch ON invoice_details(batch_id);

CREATE INDEX idx_bills_pharmacy ON bills(pharmacy_id);
CREATE INDEX idx_bills_supplier ON bills(supplier_id);
CREATE INDEX idx_bills_number ON bills(bill_number);
CREATE INDEX idx_bills_updated ON bills(updated_at);

CREATE INDEX idx_billdetails_bill ON bill_details(bill_id);
CREATE INDEX idx_billdetails_medicine ON bill_details(medicine_id);

CREATE INDEX idx_customer_returns_pharmacy ON customer_returns(pharmacy_id);
CREATE INDEX idx_customer_returns_invoice ON customer_returns(invoice_id);

CREATE INDEX idx_customer_return_details_return ON customer_return_details(return_id);
CREATE INDEX idx_customer_return_details_batch ON customer_return_details(batch_id);

CREATE INDEX idx_supplier_returns_pharmacy ON supplier_returns(pharmacy_id);
CREATE INDEX idx_supplier_returns_bill ON supplier_returns(bill_id);

CREATE INDEX idx_supplier_return_details_return ON supplier_return_details(return_id);
CREATE INDEX idx_supplier_return_details_batch ON supplier_return_details(batch_id);

CREATE INDEX idx_payments_pharmacy ON payments(pharmacy_id);
CREATE INDEX idx_inventory_logs_pharmacy ON inventory_logs(pharmacy_id);
CREATE INDEX idx_activity_logs_pharmacy ON activity_logs(pharmacy_id);

CREATE INDEX idx_changelog_pending ON change_log(pharmacy_id, changed_at);
CREATE INDEX idx_changelog_record ON change_log(table_name, record_id);
CREATE INDEX idx_changelog_sync_version ON change_log(pharmacy_id, sync_version);

CREATE INDEX idx_syncstate_status ON sync_state(sync_status);
CREATE INDEX idx_syncstate_last_sync ON sync_state(last_sync_at);

CREATE INDEX idx_id_mapping_pharmacy ON id_mapping(pharmacy_id);
CREATE INDEX idx_id_mapping_lookup ON id_mapping(pharmacy_id, table_name, local_id);
CREATE INDEX idx_id_mapping_cloud ON id_mapping(pharmacy_id, table_name, cloud_uuid);

CREATE INDEX idx_unit_type_mapping_pharmacy ON unit_type_mapping(pharmacy_id);
CREATE INDEX idx_unit_type_mapping_desktop ON unit_type_mapping(desktop_unit_type_id);

CREATE INDEX idx_conflict_log_pharmacy ON conflict_log(pharmacy_id);
CREATE INDEX idx_conflict_log_created ON conflict_log(created_at);

-- =========================================================
-- UPDATED_AT TRIGGERS
-- =========================================================
CREATE TRIGGER update_users_updated_at BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_categories_updated_at BEFORE UPDATE ON categories FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_customers_updated_at BEFORE UPDATE ON customers FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_suppliers_updated_at BEFORE UPDATE ON suppliers FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_unit_types_updated_at BEFORE UPDATE ON unit_types FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_medicines_updated_at BEFORE UPDATE ON medicines FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_batches_updated_at BEFORE UPDATE ON batches FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_batch_units_updated_at BEFORE UPDATE ON batch_units FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_med_templates_updated_at BEFORE UPDATE ON medicine_packaging_templates FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_invoices_updated_at BEFORE UPDATE ON invoices FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_invoice_details_updated_at BEFORE UPDATE ON invoice_details FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_bills_updated_at BEFORE UPDATE ON bills FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_bill_details_updated_at BEFORE UPDATE ON bill_details FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_customer_returns_updated_at BEFORE UPDATE ON customer_returns FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_customer_return_details_updated_at BEFORE UPDATE ON customer_return_details FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_supplier_returns_updated_at BEFORE UPDATE ON supplier_returns FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_supplier_return_details_updated_at BEFORE UPDATE ON supplier_return_details FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_payments_updated_at BEFORE UPDATE ON payments FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_inventory_logs_updated_at BEFORE UPDATE ON inventory_logs FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_sync_state_updated_at BEFORE UPDATE ON sync_state FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_id_mapping_updated_at BEFORE UPDATE ON id_mapping FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- =========================================================
-- CHANGE LOG TRIGGERS
-- =========================================================
CREATE TRIGGER log_users_changes BEFORE INSERT OR UPDATE OR DELETE ON users FOR EACH ROW EXECUTE FUNCTION log_table_changes();
CREATE TRIGGER log_categories_changes BEFORE INSERT OR UPDATE OR DELETE ON categories FOR EACH ROW EXECUTE FUNCTION log_table_changes();
CREATE TRIGGER log_customers_changes BEFORE INSERT OR UPDATE OR DELETE ON customers FOR EACH ROW EXECUTE FUNCTION log_table_changes();
CREATE TRIGGER log_suppliers_changes BEFORE INSERT OR UPDATE OR DELETE ON suppliers FOR EACH ROW EXECUTE FUNCTION log_table_changes();
CREATE TRIGGER log_unit_types_changes BEFORE INSERT OR UPDATE OR DELETE ON unit_types FOR EACH ROW EXECUTE FUNCTION log_table_changes();
CREATE TRIGGER log_medicines_changes BEFORE INSERT OR UPDATE OR DELETE ON medicines FOR EACH ROW EXECUTE FUNCTION log_table_changes();
CREATE TRIGGER log_batches_changes BEFORE INSERT OR UPDATE OR DELETE ON batches FOR EACH ROW EXECUTE FUNCTION log_table_changes();
CREATE TRIGGER log_batch_units_changes BEFORE INSERT OR UPDATE OR DELETE ON batch_units FOR EACH ROW EXECUTE FUNCTION log_table_changes();
CREATE TRIGGER log_med_templates_changes BEFORE INSERT OR UPDATE OR DELETE ON medicine_packaging_templates FOR EACH ROW EXECUTE FUNCTION log_table_changes();
CREATE TRIGGER log_invoices_changes BEFORE INSERT OR UPDATE OR DELETE ON invoices FOR EACH ROW EXECUTE FUNCTION log_table_changes();
CREATE TRIGGER log_invoice_details_changes BEFORE INSERT OR UPDATE OR DELETE ON invoice_details FOR EACH ROW EXECUTE FUNCTION log_table_changes();
CREATE TRIGGER log_bills_changes BEFORE INSERT OR UPDATE OR DELETE ON bills FOR EACH ROW EXECUTE FUNCTION log_table_changes();
CREATE TRIGGER log_bill_details_changes BEFORE INSERT OR UPDATE OR DELETE ON bill_details FOR EACH ROW EXECUTE FUNCTION log_table_changes();
CREATE TRIGGER log_customer_returns_changes BEFORE INSERT OR UPDATE OR DELETE ON customer_returns FOR EACH ROW EXECUTE FUNCTION log_table_changes();
CREATE TRIGGER log_customer_return_details_changes BEFORE INSERT OR UPDATE OR DELETE ON customer_return_details FOR EACH ROW EXECUTE FUNCTION log_table_changes();
CREATE TRIGGER log_supplier_returns_changes BEFORE INSERT OR UPDATE OR DELETE ON supplier_returns FOR EACH ROW EXECUTE FUNCTION log_table_changes();
CREATE TRIGGER log_supplier_return_details_changes BEFORE INSERT OR UPDATE OR DELETE ON supplier_return_details FOR EACH ROW EXECUTE FUNCTION log_table_changes();
CREATE TRIGGER log_payments_changes BEFORE INSERT OR UPDATE OR DELETE ON payments FOR EACH ROW EXECUTE FUNCTION log_table_changes();
CREATE TRIGGER log_inventory_logs_changes BEFORE INSERT OR UPDATE OR DELETE ON inventory_logs FOR EACH ROW EXECUTE FUNCTION log_table_changes();

-- =========================================================
-- INITIAL DATA
-- =========================================================
INSERT INTO roles (id, name, description, permissions) VALUES
(gen_random_uuid(), 'Administrator', 'Full system access', '{"all": true}'::jsonb),
(gen_random_uuid(), 'Pharmacist', 'Can manage medicines and sales', '{"medicines": ["view","add","edit"], "sales": ["pos","view"]}'::jsonb),
(gen_random_uuid(), 'Cashier', 'Processes sales only', '{"sales": ["pos","view"], "customers": ["view","add"]}'::jsonb),
(gen_random_uuid(), 'Manager', 'Manager with limited deletion', '{"medicines": ["view","add","edit"], "reports": ["view","export"]}'::jsonb),
(gen_random_uuid(), 'Staff', 'Basic read-only access', '{"medicines": ["view"], "sales": ["view"]}'::jsonb)
ON CONFLICT (name) DO NOTHING;

-- system unit types
INSERT INTO unit_types (id, pharmacy_id, name, is_smallest_unit, is_system, created_at)
VALUES
(gen_random_uuid(), NULL, 'Pack', FALSE, TRUE, CURRENT_TIMESTAMP),
(gen_random_uuid(), NULL, 'Strip', FALSE, TRUE, CURRENT_TIMESTAMP),
(gen_random_uuid(), NULL, 'Tablet', TRUE, TRUE, CURRENT_TIMESTAMP),
(gen_random_uuid(), NULL, 'Capsule', TRUE, TRUE, CURRENT_TIMESTAMP),
(gen_random_uuid(), NULL, 'Bottle', FALSE, TRUE, CURRENT_TIMESTAMP),
(gen_random_uuid(), NULL, 'Box', FALSE, TRUE, CURRENT_TIMESTAMP),
(gen_random_uuid(), NULL, 'Ampoule', TRUE, TRUE, CURRENT_TIMESTAMP),
(gen_random_uuid(), NULL, 'Vial', FALSE, TRUE, CURRENT_TIMESTAMP),
(gen_random_uuid(), NULL, 'Sachet', FALSE, TRUE, CURRENT_TIMESTAMP),
(gen_random_uuid(), NULL, 'Tube', FALSE, TRUE, CURRENT_TIMESTAMP),
(gen_random_uuid(), NULL, 'Inhaler', FALSE, TRUE, CURRENT_TIMESTAMP),
(gen_random_uuid(), NULL, 'Drop', TRUE, TRUE, CURRENT_TIMESTAMP)
ON CONFLICT DO NOTHING;

COMMIT;











































































