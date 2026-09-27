-- Extra Keepa evidence captured for the Product Workspace + profit calculator.
--
-- Field names verified against Keepa's official product object
-- (github.com/keepacom/api_backend, structs/Product.java):
--   * monthlySold           — "How often this product was bought in the past
--                              month ... It is not an estimate." (units)
--   * fbaFees.pickAndPackFee — FBA fulfilment fee, integer cents (0 ⇒ Keepa had
--                              no valid dimensions to compute it)
--   * referralFeePercentage  — referral fee percent for the current price
-- All three arrive on the SAME `stats`-enabled /product call Delium already
-- makes, so capturing them costs no extra Keepa tokens. Additive only.

ALTER TABLE products ADD COLUMN monthly_sold INTEGER;          -- Keepa "bought past month"
ALTER TABLE products ADD COLUMN fba_pick_pack_cents INTEGER;   -- real FBA fulfilment fee (cents)
ALTER TABLE products ADD COLUMN referral_fee_percent REAL;     -- Keepa referralFeePercentage (0-1)
