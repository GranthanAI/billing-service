# Data retention and erasure

> **FOR CHARTERED ACCOUNTANT / LAWYER REVIEW BEFORE LAUNCH.** What follows is what the code does and
> the reasoning behind it. The statutory periods are stated from memory of the Indian rules and
> must be confirmed by your CA; nothing here is legal advice.

## The rule in one sentence

When a user deletes their account we remove or anonymise everything that identifies the *person*
and we keep every record the *business* must keep: the tax invoices, credit notes, payments,
refunds, the ledger and the usage cost records.

## What happens on `user.deleted`

The `billing-user` consumer (`app/user_deletion.py`) does, in one transaction:

1. Every subscription of the user that has not ended goes to `ended` (`AccountDeleted`). That
   cancels the Razorpay mandate immediately (a `gateway.cancel` job), including a next mandate or
   a pending plan-change mandate that was waiting for its first charge.
2. The billing account is anonymised: `billing_email`, `legal_name` and `gateway_customer_id` are
   set to null.
3. A `subscription.changed` event is emitted for each ended subscription.

After the commit:

4. Every Valkey key of the user (`ent:`, `use:`, `hold:`, `idem:`, `done:`, `thr:`) is deleted and
   their open holds are removed from `holds:due`.

The event is idempotent: a replay changes nothing (step 4 runs again, harmlessly).

## What is kept, and why

| Data | Kept? | Why |
| --- | --- | --- |
| `invoices`, `invoice_lines`, credit notes (and the PDFs in object storage) | **Kept**, including the customer name and GSTIN printed on them | A tax invoice is a statutory document. GST law requires the supplier to keep invoices and credit notes (believed 72 months from the due date of the annual return for the year; **CA to confirm**) |
| `billing_accounts.state_code` and `gstin` | **Kept** | Needed to explain the place of supply and CGST/SGST/IGST on the kept invoices |
| `payments`, `refunds` (incl. the raw Razorpay payload) | **Kept** | Evidence for the invoices and for reconciliation with Razorpay and the bank. The raw payload contains the buyer's email and phone: **see the open question below** |
| `ledger_transactions`, `ledger_entries`, `ledger_balances` | **Kept** | Books of account; append-only by database triggers |
| `usage_events`, `usage_daily` | **Kept** | The cost side of margin and provider reconciliation. They carry only the pseudonymous user id, model and token counts, never message content |
| `subscription_history`, `admin_audit` | **Kept** | Audit trail; append-only |
| `billing_accounts` row itself | **Kept** (anonymised) | Foreign keys from the records above |

## What is pruned on a schedule (`housekeeping`, daily 04:30 IST)

| Table | Window |
| --- | --- |
| `processed_events` | 30 days |
| `outbox` (published rows) | 7 days |
| `idempotency_keys` | 24 hours |
| `jobs` (`done`) | 14 days |
| `webhook_inbox` (`processed`, `ignored`) | 180 days |

`webhook_inbox` rows in `received` or `failed` are never pruned automatically; they are the work
queue and the alert.

## Open questions for the CA / lawyer

1. **The raw Razorpay payload** in `payments.raw` and `webhook_inbox.payload` includes the payer's
   email and phone. Should it be redacted from stored rows on erasure (the code redacts it from
   logs and Sentry already)? If yes, add a step to `UserDeletionService` that redacts those two
   columns for the user's payments; it is a small change but it edits rows we otherwise treat as
   evidence.
2. **The customer name on issued invoices** stays (it is on the legal document). Confirm that is
   acceptable under the DPDP Act's "legal obligation" exception.
3. **Retention period** for invoices, ledger and audit rows, and what to do at the end of it (the
   code never deletes them today).
4. **`usage_events` partitions** are to be archived after 13 months (Phase 12, not built). Confirm
   13 months is enough, or that a longer period is needed for margin audits.
5. Whether a deleted user's **Razorpay customer record** should also be deleted through
   Razorpay's API (we only drop our reference to it).

## How to check an erasure by hand

```sql
SELECT billing_email, legal_name, gstin, state_code FROM billing.billing_accounts WHERE owner_id = :user;
SELECT status FROM billing.subscriptions s JOIN billing.billing_accounts a ON a.id = s.billing_account_id
 WHERE a.owner_id = :user;                      -- all 'ended'
SELECT kind, status FROM billing.jobs WHERE kind = 'gateway.cancel' ORDER BY id DESC LIMIT 5;  -- done
```

```bash
redis-cli --scan --pattern '*{<user id>}*'      # nothing
```
