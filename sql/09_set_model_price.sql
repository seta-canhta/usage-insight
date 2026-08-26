-- =============================================================================
-- 09_set_model_price.sql — populate or update a model's rates
-- =============================================================================
-- Run this whenever a model is added or a vendor price changes.
-- It NEVER updates a rate in place: it closes the current window and opens a new
-- one, so every historical cost figure stays reproducible (CONTRACT §4, design §8.4).
--
-- Usage — substitute the six @params below, then run:
--   bq query --use_legacy_sql=false \
--     --parameter='model_id:STRING:GPT-5.3-Codex' \
--     --parameter='effective_from:DATE:2026-08-01' \
--     --parameter='input_per_1k:NUMERIC:0.00000' \
--     --parameter='output_per_1k:NUMERIC:0.00000' \
--     --parameter='cached_input_per_1k:NUMERIC:0.00000' \
--     --parameter='source_url:STRING:https://<vendor pricing page>' \
--     < 09_set_model_price.sql
--
-- After running, confirm DQ-6 stops firing for this model:
--   SELECT * FROM `${PROJECT_ID}.dq.dq_findings`
--   WHERE check_id IN ('DQ-6','DQ-6c') AND DATE(detected_at) = CURRENT_DATE();
--
-- =============================================================================
-- ⚠️ READ THIS BEFORE PUBLISHING ANY COST NUMBER
-- =============================================================================
-- GitHub Copilot is not billed to the customer per token. Billing is per seat,
-- plus a premium-request allowance. Vendor per-token list prices therefore produce
-- a NOTIONAL cost — the economic weight of the tokens consumed — not an invoice
-- line you can reconcile against a bill.
--
-- That number is still the right one for the decisions this platform exists to
-- support: comparing agents, comparing models, comparing skill configurations, and
-- watching cost per accepted output move over time (design §9.1). All of those are
-- ratios and trends, where a consistent notional unit works correctly.
--
-- It is the WRONG number for: chargeback, budget reconciliation, or any figure
-- presented to finance as spend. For those, use seat count x seat price, and
-- allocate per person-month per attribution rule AR-8.
--
-- Record which interpretation you are using. Label it on the dashboard. A notional
-- cost quoted as though it were an invoice is exactly the failure mode design
-- §8.16 and §14.5 exist to prevent.
--
-- ---------------------------------------------------------------------------
-- NEW IN CONTRACT 1.1.0: the billed unit is now MEASURED
-- ---------------------------------------------------------------------------
-- Everything above was written when the premium-request count was unavailable. It
-- is not any more. Copilot's session journal reports
-- `modelMetrics.<model>.requests.cost`, which is the actual count of premium
-- requests billed, and it flows through to `premium_requests` on
-- core.fct_ai_run, marts.agg_daily_person_agent and marts.v_session_usage.
--
-- That changes what this file is for, and what it is NOT for. The rates entered
-- here still produce the notional per-token weight — the right unit for comparing
-- agents, models and skill configurations. The premium-request count is the right
-- unit for reconciling against a bill. They are DIFFERENT NUMBERS and CONTRACT
-- §4.1 forbids adding them together: "one is measured and dimensionless, the other
-- is modelled and in dollars, and a single number carrying both would be
-- defensible as neither."
--
-- Do not, therefore, be tempted to enter a "price per premium request" in the
-- columns below to make the two commensurate. There is no such column and the cost
-- formula in CONTRACT §4 has no term for it; a rate invented to bridge the two
-- would put a number in front of finance that neither source supports. DQ-BILL in
-- 07_dq_checks.sql scans view definitions for anyone who tries.
--
-- The seat component remains invisible from any client and always will — it is a
-- contract term, not telemetry. Any total presented as spend must state it is
-- excluded.
-- =============================================================================

BEGIN TRANSACTION;

-- Step 1 — close the currently-open window for this model.
-- Only touches the row with effective_to IS NULL. If none exists this is a no-op,
-- which is the correct behaviour for a model being priced for the first time.
UPDATE `${PROJECT_ID}.core.dim_model_pricing`
SET
  effective_to = DATE_SUB(@effective_from, INTERVAL 1 DAY),
  updated_at   = CURRENT_TIMESTAMP()
WHERE model_id     = @model_id
  AND effective_to IS NULL
  AND effective_from < @effective_from;   -- guard: never close a window that has
                                          -- not started, and never create an
                                          -- inverted [from, to] range.

-- Step 2 — open the new window.
INSERT INTO `${PROJECT_ID}.core.dim_model_pricing`
  (model_id, model_family, vendor,
   effective_from, effective_to,
   input_per_1k_usd, output_per_1k_usd, cached_input_per_1k_usd,
   is_placeholder, source_url, notes, updated_at)
SELECT
  @model_id,
  -- Carry family/vendor forward from the most recent row for this model so a price
  -- update does not silently drop its classification. NULL on first insert; fill it
  -- in by hand if this is a brand-new model.
  (SELECT model_family FROM `${PROJECT_ID}.core.dim_model_pricing`
    WHERE model_id = @model_id ORDER BY effective_from DESC LIMIT 1),
  (SELECT vendor       FROM `${PROJECT_ID}.core.dim_model_pricing`
    WHERE model_id = @model_id ORDER BY effective_from DESC LIMIT 1),
  @effective_from,
  NULL,                       -- open-ended until the next price change
  @input_per_1k,
  @output_per_1k,
  @cached_input_per_1k,
  FALSE,                      -- real pricing: is_placeholder = FALSE
  @source_url,
  CONCAT('Rates entered ', CAST(CURRENT_DATE() AS STRING),
         '. Notional per-token cost — see the billing note at the top of ',
         '09_set_model_price.sql before quoting this as spend.'),
  CURRENT_TIMESTAMP();

COMMIT TRANSACTION;

-- =============================================================================
-- Verification — run after the transaction commits.
-- =============================================================================
-- Expect exactly ONE open window per model, no overlaps, no inverted ranges.
--
-- SELECT
--   model_id,
--   effective_from,
--   effective_to,
--   input_per_1k_usd,
--   output_per_1k_usd,
--   is_placeholder,
--   COUNTIF(effective_to IS NULL) OVER (PARTITION BY model_id) AS open_windows
-- FROM `${PROJECT_ID}.core.dim_model_pricing`
-- ORDER BY model_id, effective_from;
--
-- Any model with open_windows <> 1 is a bug — fix it before trusting a cost figure.
