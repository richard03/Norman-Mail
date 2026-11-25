"""Synchronizace IMAP -> lokální databáze na pozadí a probouzení odložených zpráv."""

import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import bayes
import sorter
import db as db_module
import mail
from i18n import t as tr

log = logging.getLogger("mailer.sync")

# Ověření odesílatele (Authentication-Results): kolik zpráv doručené pošty musí mít nejvyšší hlavičku od stejného
# serveru, aby se mu věřilo, a jak často to přezkoumat (poskytovatel se může změnit).
AUTH_MIN_SAMPLE = 10
AUTH_SHARE = 0.8
AUTH_RECHECK = 7 * 86400

BATCH = 200                 # hlaviček na jeden FETCH
BACKFILL_BATCHES = 10       # max. dávek historie na složku v jednom cyklu (ať doručená pošta nečeká)
FULL_FLAGS_LIMIT = 5000     # do této velikosti složky kontrolujeme příznaky všech zpráv
RECENT_FLAGS = 1000         # u větších složek jen posledních N
INTERVAL = 30               # s mezi kontrolami doručené pošty
FULL_INTERVAL = 300         # s mezi synchronizacemi ostatních složek (kontext), pokud je nevyžádá akce
BUSY_INTERVAL = 3           # s mezi cykly, když se ještě dohrává historie


def uid_ranges(uids) -> str:
    """[1,2,3,7,9,10] -> "1:3,7,9:10" (kompaktní množina UID pro IMAP)."""
    parts, start, prev = [], None, None
    for u in sorted(uids):
        if start is None:
            start = prev = u
        elif u == prev + 1:
            prev = u
        else:
            parts.append(f"{start}:{prev}" if start != prev else str(start))
            start = prev = u
    if start is not None:
        parts.append(f"{start}:{prev}" if start != prev else str(start))
    return ",".join(parts)


def context_folders(folders, extra_names=()):
    """Složky, ve kterých se hledá kontext (a které se proto synchronizují).
    extra_names: uživatelské složky aplikace – synchronizují se vždy (u Gmailu jde o štítky)."""
    if any(f["role"] == "all" for f in folders):  # Gmail: štítky jsou jen pohledy na „Všechny zprávy“
        chosen = [f for f in folders if f["role"] in ("inbox", "sent", "drafts", "junk", "all")
                  or f["name"] in extra_names]
    else:
        chosen = [f for f in folders if f["role"] != "trash"]
    return sorted(chosen, key=lambda f: (f["role"] != "inbox", f["role"] == "all"))


class SyncManager:
    def __init__(self, store, db, pool):
        self.store, self.db, self.pool = store, db, pool
        self.bayes = bayes.BayesFilter(db)
        self.sorter = sorter.Sorter(db)
        self.errors = {}               # acc_id -> poslední chyba synchronizace
        self._wake = threading.Event()
        self._locks = {}
        self._locks_lock = threading.Lock()
        self._thread = None
        self._full_requested = True    # první cyklus po startu synchronizuje vše
        self._last_full = 0.0
        # Fronta operací uživatele (smazat, archivovat, spam, příznaky): UI a databáze se změní hned,
        # server dostane změnu na pozadí. Zprávy s nevyřízenou operací synchronizace nepřepisuje.
        self._ops = queue.Queue()
        self.pending = set()           # {(acc_id, folder, uid)}
        self.notices = []              # [(id, text)] – hlášení pro UI (např. selhání operace)
        self._notice_seq = 0
        self._op_thread = None

    def start(self):
        if not self._thread:
            self._thread = threading.Thread(target=self._loop, name="mail-sync", daemon=True)
            self._thread.start()
        if not self._op_thread:
            self._op_thread = threading.Thread(target=self._op_worker, name="mail-ops", daemon=True)
            self._op_thread.start()

    # ------------------------------------------------------------------ fronta operací

    def enqueue(self, acc, folder, uid, label, fn, remove=False, on_error=None, use_conn=True):
        """Zařadí operaci se zprávou. fn(M) běží na pozadí pod zámkem spojení synchronizace
        (use_conn=False: fn() bez spojení – např. přesun mezi účty, který spojení střídá sám).
        remove=True: zpráva ze složky zmizí (po dokončení se pro jistotu odebere i z databáze)."""
        uids = list(uid) if isinstance(uid, (list, tuple, set)) else [uid]  # i více zpráv najednou
        keys = [(acc["id"], folder, u) for u in uids]
        self.pending.update(keys)
        self._ops.put((acc, keys, label, fn if use_conn else (lambda M, f=fn: f()), remove, on_error, use_conn))
        if not self._op_thread:  # např. v testech bez start()
            self._run_op(self._ops.get())

    def _op_worker(self):
        while True:
            self._run_op(self._ops.get())

    def _run_op(self, item):
        acc, keys, label, fn, remove, on_error, use_conn = item
        try:
            if use_conn:
                with self.pool.get(acc) as M:
                    fn(M)
            else:
                fn(None)
            if remove:
                left = [k for k in keys if self.db.message(*k)]
                if left:
                    self.db.delete_uids(acc["id"], keys[0][1], [k[2] for k in left])
                    self.db.bump()
        except Exception as e:
            msg = tr("err.opFailed", action=label, error=mail.friendly_error(e))
            log.warning(msg)
            self.notice(msg)
            if on_error:
                on_error(msg)
        finally:
            for k in keys:
                self.pending.discard(k)
        self.trigger()  # srovnat se serverem (při chybě se zpráva vrátí)

    def notice(self, text):
        self._notice_seq += 1
        self.notices = (self.notices + [(self._notice_seq, text)])[-20:]

    def _pending_uids(self, acc_id, folder):
        return {u for a, f, u in list(self.pending) if a == acc_id and f == folder}

    def trigger(self):
        """Spustí další (úplný) cyklus hned – např. po akci uživatele nebo kliknutí na Obnovit."""
        self._full_requested = True
        self._wake.set()

    def _folder_lock(self, acc_id, folder):
        with self._locks_lock:
            return self._locks.setdefault((acc_id, folder), threading.Lock())

    # ------------------------------------------------------------------ hlavní smyčka

    def _sync_inbox(self, acc):
        """1. fáze pro jeden účet: seznam složek a doručená pošta."""
        try:
            folders = self.refresh_folders(acc)
            self.sync_folder(acc, "INBOX")
            self.errors.pop(acc["id"], None)
            return folders
        except Exception as e:
            self._error(acc, e)
            return []

    def _loop(self):
        busy = False
        while True:
            full = self._full_requested or busy or time.time() - self._last_full >= FULL_INTERVAL
            self._full_requested = False
            busy = False
            try:
                self.process_snoozes()
                accounts = self.store.all()
                # 1. fáze: doručená pošta všech účtů paralelně (pomalé přihlášení jednoho nebrzdí ostatní)
                with ThreadPoolExecutor(max_workers=max(1, min(8, len(accounts)))) as ex:
                    folder_sets = dict(zip([a["id"] for a in accounts], ex.map(self._sync_inbox, accounts)))
                # 2. fáze: ostatní složky (kvůli kontextu), historie po dávkách
                if full:
                    for acc in accounts:
                        for f in folder_sets.get(acc["id"], []):
                            if f["name"] == "INBOX":
                                continue
                            try:
                                busy |= not self.sync_folder(acc, f["name"], max_batches=BACKFILL_BATCHES)
                            except Exception as e:
                                self._error(acc, e)
                                break
                    self._last_full = time.time()
                self.process_snoozes()
                self.cleanup_categories()
                self.apply_folder_rules()
                if full:
                    self.sorter.prune()
                    self.bayes_maintenance()
            except Exception:
                log.exception("Chyba synchronizace")
            self._wake.wait(BUSY_INTERVAL if busy else INTERVAL)
            self._wake.clear()

    def _error(self, acc, e):
        self.errors[acc["id"]] = mail.friendly_error(e)
        log.warning("Synchronizace účtu %s: %s", acc["name"], self.errors[acc["id"]])

    def refresh_folders(self, acc):
        with self.pool.get(acc) as M:
            folders = mail.list_folders(M, with_counts=False)
        self.db.upsert_folders(acc["id"], folders)
        chosen = context_folders(folders, self.db.user_folder_names(acc["id"]))
        # koš se synchronizuje kvůli složce Smazané (do kontextu ale nepatří)
        return chosen + [f for f in folders if f["role"] == "trash" and f not in chosen]

    # ------------------------------------------------------------------ synchronizace složky

    def sync_folder(self, acc, folder, max_batches=None) -> bool:
        """Srovná databázi se serverem. Vrací True, pokud je složka kompletně stažená."""
        acc_id = acc["id"]
        with self._folder_lock(acc_id, folder):
            changed = False
            with self.pool.get(acc) as M:
                status = mail.folder_status(M, folder)
                state = self.db.folder(acc_id, folder) or {}
                if state.get("uidvalidity") != status["uidvalidity"]:
                    # UID už neplatí (složka znovu vytvořena apod.) – cache složky zahodit
                    self.db.clear_folder(acc_id, folder)
                    self.db.set_folder_state(acc_id, folder, uidvalidity=status["uidvalidity"], complete=0)
                    changed = True
                mail._select(M, folder, readonly=True)
                server = mail.all_uids(M)
                cached = self.db.cached_flags(acc_id, folder)

                gone = cached.keys() - server
                if gone:
                    self.db.delete_uids(acc_id, folder, gone)
                    changed = True

                pending = self._pending_uids(acc_id, folder)
                present = sorted((cached.keys() & server) - pending)
                if present:
                    if len(present) > FULL_FLAGS_LIMIT:
                        present = present[-RECENT_FLAGS:]
                    flags = mail.fetch_flags(M, uid_ranges(present))
                    deleted = [u for u, (_, _, d) in flags.items() if d and u in cached]
                    updates = [(u, s, f) for u, (s, f, d) in flags.items()
                               if u in cached and not d and cached[u] != (s, f)]
                    if deleted:
                        self.db.delete_uids(acc_id, folder, deleted)
                    if updates:
                        self.db.update_flags(acc_id, folder, updates)
                    changed |= bool(deleted or updates)

            missing = sorted(server - cached.keys() - self._pending_uids(acc_id, folder), reverse=True)  # nejnovější první
            rules = self.db.spam_senders() if folder == "INBOX" else set()
            bayes_on = folder == "INBOX" and self.bayes.active()
            # pravidla složek v režimu „ihned“ – nové zprávy přesunout dřív, než se ukážou v seznamu
            order = {k: i for i, k in enumerate(db_module.RULE_KINDS)}  # odesílatel > příjemce > předmět
            frules = sorted((r for r in self.db.folder_rules_active() if not r["after_read"]),
                            key=lambda r: order[r["kind"]]) if folder == "INBOX" else []
            batches = [missing[i:i + BATCH] for i in range(0, len(missing), BATCH)]
            for n, chunk in enumerate(batches):
                if max_batches is not None and n >= max_batches:
                    break
                with self.pool.get(acc) as M:  # zámek spojení se uvolňuje mezi dávkami
                    mail._select(M, folder, readonly=True)
                    records = mail.fetch_header_records(M, chunk)
                held = []
                if folder == "INBOX":  # ručně umístěné zprávy (např. vrácené ze složky) automatika nepřesouvá
                    locked = self.db.locked_keys(records)
                    held = [r for r in records if db_module.message_key(r) in locked]
                    records = [r for r in records if db_module.message_key(r) not in locked]
                if rules:
                    records = self._apply_spam_rules(acc, folder, records, rules, frules)
                if bayes_on:
                    records = self._apply_bayes(acc, records)
                if frules:
                    records = self._apply_folder_rules_new(acc, records, frules)
                if folder == "INBOX":
                    records = self._apply_sorter(acc, records)
                records = held + records
                self.db.insert_messages(acc_id, folder, records)
                changed = True
                if n == 0 and folder == "INBOX":
                    self.db.bump()  # první dávka doručené pošty – ukázat co nejdřív

            complete = max_batches is None or len(batches) <= max_batches
            self.db.set_folder_state(acc_id, folder, complete=int(complete), last_sync=time.time())
            if changed:
                self.db.bump()
        if folder == self.db.trash_folders().get(acc_id):
            self._mark_folder_read(acc, folder)
        return complete

    # ------------------------------------------------------------------ koš („Smazané“)

    def _mark_folder_read(self, acc, folder):
        """Zprávy v koši jsou vždy přečtené – i ty, které tam přesunul jiný klient."""
        uids = [u for u in self.db.unseen_uids(acc["id"], folder) if (acc["id"], folder, u) not in self.pending]
        if not uids:
            return
        try:
            with self.pool.get(acc) as M:
                mail.mark_seen_uids(M, folder, uids)
        except Exception as e:
            self._error(acc, e)
            return
        self.db.set_seen_bulk(acc["id"], folder, uids)
        self.db.bump()

    def trash_messages(self, acc, folder, uids):
        """Smazání zpráv (do koše, označené jako přečtené; z koše natrvalo) a stažení koše,
        ať jsou zprávy hned ve složce Smazané."""
        with self.pool.get(acc) as M:
            mail.trash_uids(M, folder, uids)
        trash = self.db.trash_folders().get(acc["id"])
        if trash and trash != folder:
            self._sync_target(acc, trash)

    def spam_messages(self, acc, folder, uids):
        """Přesun zpráv do spamu (bez hromadného přesunu starších zpráv odesílatele) a stažení spamu."""
        with self.pool.get(acc) as M:
            self.bayes_learn(M, acc, folder, uids, True)
            mail.move_to_spam(M, folder, uids)
        if acc["id"] not in self.db.spam_folders():
            self.refresh_folders(acc)  # složka Spam právě vznikla
        junk = self.db.spam_folders().get(acc["id"])
        if junk and junk != folder:
            self._sync_target(acc, junk)

    def empty_trash(self, acc, trash) -> int:
        """Natrvalo smaže všechny zprávy v koši účtu. Pod zámkem složky, ať je synchronizace mezitím nevrátí."""
        with self._folder_lock(acc["id"], trash):
            with self.pool.get(acc) as M:
                n = mail.expunge_all(M, trash)
            self.db.clear_folder(acc["id"], trash)
        self.db.bump()
        return n

    def _apply_spam_rules(self, acc, folder, records, rules, frules=()):
        """Nové zprávy od odesílatelů z pravidel přesune do spamu (dřív, než se ukážou v seznamu).
        Zprávy, které řadí jiné explicitní pravidlo (kategorie, složka), mají přednost – Spam je
        nejméně důležitý cíl. Vrací zbylé záznamy."""
        hits = {}
        category_of = self.db.categorizer(marks=False)
        for r in records:
            sender = next((a["addr"].lower() for a in r["from"] if a["addr"].lower() in rules), None)
            if sender and (category_of(r) or any(db_module.rule_matches(x, r) for x in frules)):
                continue
            if sender and not r.get("deleted"):
                hits.setdefault(sender, []).append(r["uid"])
        if not hits:
            return records
        uids = [u for lst in hits.values() for u in lst]
        try:
            with self.pool.get(acc) as M:
                mail.move_to_spam(M, folder, uids)
        except Exception as e:  # zprávy zůstanou v doručené poště, zkusí se příště
            self._error(acc, e)
            return records
        for sender, lst in hits.items():
            self.db.add_rule_hits(sender, len(lst))
        log.info("Pravidla pro spam: přesunuto %d zpráv (účet %s)", len(uids), acc["name"])
        moved = set(uids)
        return [r for r in records if r["uid"] not in moved]

    # ------------------------------------------------------------------ bayesovský filtr

    def _apply_bayes(self, acc, records):
        """Čerstvé nepřečtené zprávy posoudí bayesovským filtrem; rozpoznaný spam přesune do spamu.
        Vrací zbylé záznamy. Lidé, kterým jsem psal, zdroje notifikací a nedůležitých zpráv a zprávy pro složky se neposuzují."""
        now = time.time()
        fresh = [r for r in records if not r.get("deleted") and not r["seen"] and (r["date"] or now) > now - bayes.RECENT]
        if not fresh:
            return records
        sender = lambda r: r["from"][0]["addr"].lower() if r["from"] else ""
        safe = self.db.safe_senders({sender(r) for r in fresh})
        trusted = self._trusted_authserv(acc) if safe else None
        # Adresu v Od lze podvrhnout: „bezpečný“ odesílatel se filtru vyhne, jen když ho ověřil poskytovatel
        # schránky (DMARC/DKIM). Poskytovatel, který ověření nezapisuje, nedává jak rozlišit – tam jako dřív.
        verified = (lambda r: r.get("auth_ok") and r.get("auth_id") == trusted) if trusted else (lambda r: True)
        frules = self.db.folder_rules_active()
        category_of = self.db.categorizer(marks=False)  # explicitní pravidla mají přednost před filtrem
        cands = [r for r in fresh if not (sender(r) in safe and verified(r))
                 and not any(db_module.rule_matches(x, r) for x in frules) and not category_of(r)]
        if not cands:
            return records
        try:
            with self.pool.get(acc) as M:
                mail._select(M, "INBOX", readonly=True)
                tokens = bayes.fetch_tokens(M, [r["uid"] for r in cands])
        except Exception as e:
            log.warning("Bayes: zprávy nejde stáhnout (%s): %s", acc["name"], e)
            return records
        threshold, spam = self.bayes.threshold(), []
        for r in cands:
            t = tokens.get(r["uid"])
            if t is None:
                continue
            score = self.bayes.score(t)
            moved = score is not None and score >= threshold
            self.bayes.remember(bayes.record_key(r), acc["id"], r["message_id"], t, score, moved)
            if moved:
                spam.append(r["uid"])
        if not spam:
            return records
        try:
            with self.pool.get(acc) as M:
                mail.move_to_spam(M, "INBOX", spam)
        except Exception as e:
            self._error(acc, e)
            return records
        log.info("Bayes: přesunuto do spamu %d zpráv (účet %s)", len(spam), acc["name"])
        moved = set(spam)
        return [r for r in records if r["uid"] not in moved]

    def _apply_sorter(self, acc, records):
        """Nové zprávy, které neřadí ruční umístění ani explicitní pravidlo, zařadí filtr podobnosti, když si
        je dost jistý: do Nedůležitých / Notifikací (podle nastavení), do složky jen se zapnutým „řadit
        automaticky“. Vrací zbylé záznamy (bez zpráv přesunutých do složek)."""
        try:
            if not self.sorter.ready():
                return records
            settings = self.db.settings()
            folders = {f"folder:{f['id']}": f for f in self.db.user_folders() if f["account_id"] and f["auto_sort"]}
            explicit = self.db.categorizer(marks=False)
            to_folder = {}
            for r in records:
                if r.get("deleted") or explicit(r):
                    continue
                guess = self.sorter.predict(r)
                if not guess or guess[1] < sorter.AUTO_THRESHOLD:
                    continue
                cls, score = guess
                if cls in db_module.CATEGORIES and settings.get(f"sorter_{cls}"):
                    self.sorter.remember(r, cls, score)
                elif cls in folders:
                    to_folder.setdefault(cls, []).append((r, score))
        except Exception:
            log.exception("Filtr podobnosti selhal")
            return records
        moved = set()
        for cls, items in to_folder.items():
            try:
                done = set(self.move_messages(acc, "INBOX", [r["uid"] for r, _ in items], folders[cls]))
            except Exception as e:  # zůstanou v doručené poště
                self._error(acc, e)
                continue
            for r, score in items:
                if r["uid"] in done:
                    self.sorter.remember(r, cls, score)
            moved |= done
            log.info("Filtr podobnosti: do složky %s přesunuto %d zpráv", folders[cls]["name"], len(done))
        return [r for r in records if r["uid"] not in moved]

    def _trusted_authserv(self, acc):
        """authserv-id serveru, který u tohoto účtu zapisuje Authentication-Results (např. mx.google.com),
        nebo None, když to poskytovatel nedělá. Zjistí se z posledních zpráv doručené pošty: musí ho mít
        nejvyšší hlavička aspoň u AUTH_SHARE z nich. Výsledek platí AUTH_RECHECK."""
        key = "authserv:" + acc["id"]
        cached = self.db.meta_get(key)
        if cached and time.time() - cached.get("ts", 0) < AUTH_RECHECK:
            return cached.get("id")
        try:
            with self.pool.get(acc) as M:
                aid, hits, total = mail.probe_authserv(M)
        except Exception as e:
            log.warning("Ověřování odesílatelů nejde zjistit (%s): %s", acc["name"], e)
            return cached.get("id") if cached else None
        trusted = aid if total >= AUTH_MIN_SAMPLE and hits >= AUTH_SHARE * total else None
        self.db.meta_set(key, {"id": trusted, "ts": time.time()})
        log.info("Ověřování odesílatelů (%s): %s (%d z %d zpráv)", acc["name"], trusted or "poskytovatel neověřuje",
                 hits, total)
        return trusted

    def bayes_learn(self, M, acc, folder, uids, is_spam):
        """Učení z akce uživatele (Spam / Není spam). Chyba učení nesmí zastavit samotný přesun."""
        try:
            self.bayes.learn(M, acc["id"], folder, uids, is_spam)
        except Exception:
            log.exception("Bayes: učení selhalo")

    def bayes_maintenance(self):
        try:
            self.bayes.mature()
        except Exception:
            log.exception("Bayes: údržba selhala")
        if self.bayes.needs_bootstrap() and self.bayes.bootstrap_state["status"] != "running" \
                and self.db._q("SELECT 1 FROM messages WHERE folder='INBOX' LIMIT 1"):
            self.start_bayes_bootstrap()

    def start_bayes_bootstrap(self):
        self.bayes.bootstrap_state = {"status": "running", "done": 0, "total": 0}
        threading.Thread(target=self.bayes.bootstrap, args=(self.store, self.pool),
                         name="bayes-bootstrap", daemon=True).start()

    # ------------------------------------------------------------------ uživatelské složky

    def move_messages(self, src_acc, src_folder, uids, target, learn_ham=False):
        """Přesune zprávy do cílové složky aplikace. Vrací seznam přesunutých UID.
        Mezi účty: stáhnout ze zdroje → vložit do cíle → teprve pak smazat ze zdroje (do koše).
        Zámky spojení obou účtů se nikdy nedrží současně (žádné uváznutí)."""
        uids = list(uids)
        if not uids:
            return []
        imap_name, mark_read = target["imap_name"], bool(target["mark_read"])
        if learn_ham:  # ze spamu ven = „Není spam“
            with self.pool.get(src_acc) as M:
                self.bayes_learn(M, src_acc, src_folder, uids, False)
        if target["account_id"] == src_acc["id"]:
            with self.pool.get(src_acc) as M:
                mail.move_to_named_folder(M, src_folder, uids, imap_name, mark_read)
            self._sync_target(src_acc, imap_name)
            return uids
        dst_acc = self.store.get(target["account_id"])
        if dst_acc is None:
            raise mail.MailError(tr("err.targetAccountGone", folder=target["name"]))
        with self.pool.get(src_acc) as M:
            sizes = mail.message_sizes(M, src_folder, uids)
        too_big = [u for u in uids if sizes.get(u, 0) > mail.MAX_MESSAGE]
        done, error = [], None
        # po dávkách omezených velikostí – v paměti nikdy víc než TRANSFER_BATCH_BYTES (+ jedna zpráva)
        for batch in mail.size_batches([u for u in uids if u not in too_big], sizes):
            with self.pool.get(src_acc) as M:
                items = mail.fetch_for_transfer(M, src_folder, batch)
            with self.pool.get(dst_acc) as M:
                ok, error = mail.append_messages(M, imap_name, items, mark_read)
            del items
            if ok:
                with self.pool.get(src_acc) as M:
                    mail.trash_uids(M, src_folder, ok)
                done += ok
            if error:
                break
        if done:
            self._sync_target(dst_acc, imap_name)
        if error:
            raise error
        if too_big:
            raise mail.MessageTooLarge(tr("err.transferTooLarge", count=len(too_big),
                                          limit=mail.MAX_MESSAGE // 1024 // 1024))
        return done

    def _apply_folder_rules_new(self, acc, records, frules):
        """Nově stažené zprávy z doručené pošty přesune podle pravidel složek („ihned“). Vrací zbylé záznamy."""
        groups = {}  # folder_id -> (složka, {uid: id pravidla})
        for r in records:
            if r.get("deleted"):
                continue
            rule = next((x for x in frules if db_module.rule_matches(x, r)), None)
            if rule:
                groups.setdefault(rule["id"], (rule, {}))[1][r["uid"]] = rule["rule_id"]
        if not groups:
            return records
        moved = set()
        for f, uids in groups.values():
            try:
                done = self.move_messages(acc, "INBOX", uids, f)
            except Exception as e:  # zůstanou v doručené poště, zkusí se příště
                self._error(acc, e)
                continue
            moved |= set(done)
            self._count_hits({u: uids[u] for u in done})
        return [r for r in records if r["uid"] not in moved]

    def apply_folder_rules(self):
        """Přesune zprávy z doručené pošty (všech účtů) podle pravidel složek – i ty, které už v databázi
        jsou (např. po přidání pravidla nebo po přečtení v režimu „až po přečtení“)."""
        for f in self.db.user_folders():
            if not f["account_id"]:
                continue  # složka z dřívější verze bez cílového účtu – čeká na dokonfigurování
            for acc_id, uids in self.db.folder_rule_matches(f["id"]).items():
                uids = {u: s for u, s in uids.items() if (acc_id, "INBOX", u) not in self.pending}
                acc = self.store.get(acc_id)
                if not uids or acc is None:
                    continue
                keys = {(acc_id, "INBOX", u) for u in uids}
                self.pending |= keys  # synchronizace je mezitím nevrátí
                try:
                    done = self.move_messages(acc, "INBOX", uids, f)
                    self.db.delete_uids(acc_id, "INBOX", done)
                except Exception as e:
                    self._error(acc, e)
                    continue
                finally:
                    self.pending -= keys
                self._count_hits({u: uids[u] for u in done})
                log.info("Složka %s: přesunuto %d zpráv (z účtu %s)", f["name"], len(done), acc["name"])
                self.db.bump()

    def _count_hits(self, uid_to_rule):
        counts = {}
        for rule_id in uid_to_rule.values():
            counts[rule_id] = counts.get(rule_id, 0) + 1
        for rule_id, n in counts.items():
            self.db.add_folder_rule_hits(rule_id, n)

    def delete_server_folder(self, acc, imap_name, label):
        """Smaže složku na serveru i se zprávami (na pozadí); při chybě hlášení do UI."""
        try:
            with self.pool.get(acc) as M:
                mail.delete_folder_with_messages(M, imap_name)
        except Exception as e:
            self.notice(tr("err.folderDeleteFailed", folder=label, error=mail.friendly_error(e)))
            self.trigger()
            return
        self.db.clear_folder(acc["id"], imap_name)
        self.db.bump()
        self.trigger()  # seznam složek účtu

    def _sync_target(self, acc, imap_name):
        """Po přesunu stáhnout cílovou složku, ať jsou zprávy hned vidět ve složce."""
        try:
            self.sync_folder(acc, imap_name, max_batches=BACKFILL_BATCHES)
        except Exception as e:
            self._error(acc, e)

    def ensure_synced(self, acc, folder="INBOX"):
        """Při prvním zobrazení složky stáhne alespoň nejnovější dávku (synchronně)."""
        state = self.db.folder(acc["id"], folder)
        if not state or not state.get("last_sync"):
            self.sync_folder(acc, folder, max_batches=1)
            self.trigger()

    def context_ready(self, acc_id) -> bool:
        folders = context_folders(self.db.folders(acc_id), self.db.user_folder_names(acc_id))
        return bool(folders) and all(f["complete"] and f["last_sync"] for f in folders)

    # ------------------------------------------------------------------ úklid Notifikací a Nedůležitých

    CATEGORY_SETTINGS = {"notifications": "notify", "unimportant": "unimp"}  # předpona klíčů nastavení
    CATEGORY_LOG = {"notifications": "notifikací", "unimportant": "nedůležitých zpráv"}

    def cleanup_categories(self):
        """Podle nastavení smaže (do koše) staré notifikace, nedůležité zprávy a zprávy ve spamu."""
        settings = self.db.settings()
        now = time.time()
        work = []  # (účet, složka, uid, popis)
        for cat, prefix in self.CATEGORY_SETTINGS.items():
            for seen in (True, False):
                age = db_module.delete_age(settings, prefix, seen)
                if age is None:
                    continue
                what = f"{'přečtených' if seen else 'nepřečtených'} {self.CATEGORY_LOG[cat]}"
                for acc_id, uids in self.db.category_uids(cat, seen=seen, older_than=now - age).items():
                    work.append((acc_id, "INBOX", uids, what, age))
        age = db_module.delete_age(settings, "spam")
        if age is not None:
            for acc_id, (folder, uids) in self.db.role_uids_older("junk", now - age).items():
                work.append((acc_id, folder, uids, "zpráv ve spamu", age))
        for acc_id, folder, uids, what, age in work:
            acc = self.store.get(acc_id)
            if acc is None:
                continue
            try:
                with self.pool.get(acc) as M:
                    mail.trash_uids(M, folder, uids)
                    self.db.delete_uids(acc_id, folder, uids)
            except Exception as e:
                self._error(acc, e)
                continue
            log.info("Smazáno %d %s starších než %s (účet %s)", len(uids), what,
                     "týden" if age == db_module.WEEK else "měsíc", acc["name"])
            self.db.bump()

    def mark_category_read(self, category):
        """Označí všechny zprávy kategorie jako přečtené – na serveru i v databázi. Zápis na server běží pod
        zámkem spojení synchronizace, aby ho souběžná kontrola příznaků nepřepsala starým stavem."""
        total = 0
        for acc_id, uids in self.db.category_uids(category, seen=False).items():
            acc = self.store.get(acc_id)
            if acc is None:
                continue
            try:
                with self.pool.get(acc) as M:
                    mail.mark_seen_uids(M, "INBOX", uids)
                    self.db.set_seen_bulk(acc_id, "INBOX", uids)
                total += len(uids)
            except Exception as e:
                self._error(acc, e)
        if total:
            self.db.bump()
        return total

    # ------------------------------------------------------------------ odložené zprávy

    def process_snoozes(self):
        for s in self.db.due_snoozes():
            acc = self.store.get(s["account_id"])
            if acc is None:
                self.db.remove_snooze(s["account_id"], s["folder"], s["uid"])
                continue
            try:
                with self.pool.get(acc) as M:
                    uid = mail.mark_unseen_by_id(M, s["folder"], s["uid"], s["message_id"])
            except Exception as e:  # síť apod. – zkusíme v dalším cyklu
                self._error(acc, e)
                continue
            self.db.remove_snooze(s["account_id"], s["folder"], s["uid"])
            if uid is not None:
                self.db.set_flag(s["account_id"], s["folder"], uid, "seen", False)
            self.db.bump()
