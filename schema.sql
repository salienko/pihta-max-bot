CREATE TABLE IF NOT EXISTS bath_resource (id integer PRIMARY KEY CHECK(id=1));
INSERT INTO bath_resource VALUES(1) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS staff (
 telegram_id bigint PRIMARY KEY, role text NOT NULL CHECK(role IN ('owner','admin'))
);
CREATE TABLE IF NOT EXISTS settings (key text PRIMARY KEY, value jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS holiday (
 day date PRIMARY KEY, base bigint NOT NULL CHECK(base>0 AND base%2=0),
 extra bigint NOT NULL CHECK(extra>0), label text NOT NULL, actor bigint NOT NULL
);
CREATE TABLE IF NOT EXISTS service_catalog (
 code text PRIMARY KEY, name text NOT NULL, price bigint NOT NULL CHECK(price>=0), active boolean NOT NULL DEFAULT true
);
CREATE TABLE IF NOT EXISTS booking (
 id text PRIMARY KEY, operation_key text UNIQUE NOT NULL, guest_id bigint NOT NULL,
 name text NOT NULL, phone text NOT NULL, start_at timestamptz NOT NULL, end_at timestamptz NOT NULL,
 cleanup_end timestamptz NOT NULL, status text NOT NULL CHECK(status IN
 ('held','payment_review','review_overdue','confirmed','in_progress','completed','expired','rejected','cancelled')),
 guests integer NOT NULL CHECK(guests BETWEEN 1 AND 8),
 children integer NOT NULL CHECK(children>=0 AND children<=guests),
 rent bigint NOT NULL CHECK(rent>=0), price_lines jsonb NOT NULL,
 rules_hash text NOT NULL, consent_at timestamptz NOT NULL,
 release_at timestamptz NOT NULL, claim_at timestamptz, verification_due timestamptz,
 revision integer NOT NULL DEFAULT 1, created_at timestamptz NOT NULL DEFAULT now(),
 CHECK(start_at<end_at AND cleanup_end=end_at+interval '1 hour')
);
CREATE INDEX IF NOT EXISTS booking_calendar ON booking(start_at,cleanup_end);
CREATE TABLE IF NOT EXISTS calendar_block (
 id text PRIMARY KEY, start_at timestamptz NOT NULL, end_at timestamptz NOT NULL,
 reason text NOT NULL, actor bigint NOT NULL, active boolean NOT NULL DEFAULT true,
 CHECK(start_at<end_at)
);
CREATE TABLE IF NOT EXISTS payment_claim (
 id text PRIMARY KEY, booking_id text NOT NULL REFERENCES booking(id),
 amount bigint NOT NULL CHECK(amount>0), status text NOT NULL DEFAULT 'pending'
 CHECK(status IN ('pending','confirmed','rejected')), created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_claim ON payment_claim(booking_id) WHERE status='pending';
CREATE TABLE IF NOT EXISTS payment_link_request (
 id text PRIMARY KEY, booking_id text NOT NULL REFERENCES booking(id),
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sent','closed')),
 url text, actor bigint, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_link ON payment_link_request(booking_id) WHERE status='pending';
CREATE TABLE IF NOT EXISTS receipt (
 id text PRIMARY KEY, booking_id text NOT NULL REFERENCES booking(id),
 claim_id text UNIQUE REFERENCES payment_claim(id), amount bigint NOT NULL CHECK(amount>0),
 actor bigint NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS booking_service (
 id text PRIMARY KEY, booking_id text NOT NULL REFERENCES booking(id), code text NOT NULL,
 name text NOT NULL, quantity integer NOT NULL CHECK(quantity BETWEEN 1 AND 100),
 price bigint NOT NULL CHECK(price>=0), status text NOT NULL DEFAULT 'requested'
 CHECK(status IN ('requested','approved','rejected')), actor bigint,
 operation_key text UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS change_request (
 id text PRIMARY KEY, booking_id text NOT NULL REFERENCES booking(id), kind text NOT NULL
 CHECK(kind IN ('cancel','move','no_show')), target text NOT NULL DEFAULT '',
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
 actor bigint, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_change ON change_request(booking_id) WHERE status='pending';
CREATE TABLE IF NOT EXISTS refund (
 id text PRIMARY KEY, booking_id text NOT NULL REFERENCES booking(id), request_id text UNIQUE,
 amount bigint NOT NULL CHECK(amount>=0), status text NOT NULL DEFAULT 'manual_review',
 reason text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS audit (
 id bigserial PRIMARY KEY, actor bigint NOT NULL, action text NOT NULL,
 entity_id text NOT NULL, details jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ui_session (
 telegram_id bigint PRIMARY KEY, state text NOT NULL, data jsonb NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS guest_contact (
 telegram_id bigint PRIMARY KEY, phone text NOT NULL, updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS outbox (
 id bigserial PRIMARY KEY, chat_id bigint NOT NULL, text text NOT NULL,
 booking_id text REFERENCES booking(id), revision integer, kind text,
 due_at timestamptz NOT NULL DEFAULT now(), sent_at timestamptz, attempts integer NOT NULL DEFAULT 0,
 locked_until timestamptz, last_error text, dedupe_key text UNIQUE NOT NULL
);

ALTER TABLE booking ADD COLUMN IF NOT EXISTS duration_approved boolean NOT NULL DEFAULT true;
CREATE TABLE IF NOT EXISTS checkout_draft (
 telegram_id bigint PRIMARY KEY, token text NOT NULL, data jsonb NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS duration_request (
 id text PRIMARY KEY, booking_id text NOT NULL REFERENCES booking(id),
 target_hours integer NOT NULL CHECK(target_hours BETWEEN 3 AND 5),
 initial boolean NOT NULL DEFAULT false, status text NOT NULL DEFAULT 'pending'
 CHECK(status IN ('pending','approved','rejected')), actor bigint, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_duration ON duration_request(booking_id) WHERE status='pending';
ALTER TABLE duration_request ADD COLUMN IF NOT EXISTS quoted_rent bigint NOT NULL DEFAULT 0;
