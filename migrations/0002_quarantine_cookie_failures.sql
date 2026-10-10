-- The reconcile loop (plugin.py::_cookie_reconnect_loop) used to lump three
-- very different outcomes into one "failed" counter: a cookie whose
-- ciphertext predates a Fernet key rotation (permanently undecryptable), a
-- cookie Chromium structurally rejects (e.g. SameSite=None without Secure),
-- and a plain transient CDP/websocket hiccup. Logged at INFO every 15s,
-- forever, with no way to tell "this needs a human" from "this is normal
-- network noise".
--
-- Quarantine makes the first two kinds visible and permanent (never
-- silently deleted — the row, and its ciphertext, stay for inspection and
-- for the case the old key ever resurfaces) instead of re-attempted forever.
-- fail_streak lets a CDP rejection prove itself non-transient (3 consecutive
-- rejections) before being quarantined, rather than quarantining on the
-- first failure.
--
-- On a fresh install the table is created in this shape already by
-- ensure_table() (migrations run after plugin.activate()), so these
-- ADD COLUMN IF NOT EXISTS are a no-op there — harmless, same pattern as
-- 0001.

ALTER TABLE app__proxy__persisted_cookies ADD COLUMN IF NOT EXISTS quarantined_at DOUBLE PRECISION;
ALTER TABLE app__proxy__persisted_cookies ADD COLUMN IF NOT EXISTS quarantine_reason TEXT;
ALTER TABLE app__proxy__persisted_cookies ADD COLUMN IF NOT EXISTS fail_streak INTEGER NOT NULL DEFAULT 0;
