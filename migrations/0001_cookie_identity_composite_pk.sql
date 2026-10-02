-- Cookie identity per RFC 6265 §5.3 is (name, domain, path), not name alone.
-- A name-only PRIMARY KEY let same-name cookies from different domains
-- (e.g. .google.com / accounts.google.com / mail.google.com) silently
-- overwrite each other — the last sync won, leaving an incomplete restored
-- set. Widen the key to the full identity tuple.
--
-- Existing rows were already constrained to one-row-per-name, so there is
-- no duplicate-key risk in widening — this only prevents future collisions.
-- On a fresh install the table is created in this shape already by
-- ensure_table() (migrations run after plugin.activate()), so the DROP+ADD
-- below just recreates an identical constraint — harmless.

UPDATE app__proxy__persisted_cookies SET domain = '' WHERE domain IS NULL;
UPDATE app__proxy__persisted_cookies SET path = '/' WHERE path IS NULL;

ALTER TABLE app__proxy__persisted_cookies ALTER COLUMN domain SET NOT NULL;
ALTER TABLE app__proxy__persisted_cookies ALTER COLUMN path SET NOT NULL;

ALTER TABLE app__proxy__persisted_cookies DROP CONSTRAINT app__proxy__persisted_cookies_pkey;
ALTER TABLE app__proxy__persisted_cookies ADD PRIMARY KEY (name, domain, path);
