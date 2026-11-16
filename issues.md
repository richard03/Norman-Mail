# Bezpečnostní issues

Nálezy z bezpečnostních auditů (7. 11. a 16. 11. 2026), seřazené od nejzávažnějších. Řešíme je jeden po druhém;
vyřešené issue se ze souboru smaže.

---

## 14. Našeptávání adres lze ovlivnit cizím mailem (nízká)

**Problém:** Kontakty se učí i z přijaté pošty od lidí, kterým jste nikdy nepsali (s nízkou váhou).
Útočník pošle mail se jménem „Jan Novák“ ze své adresy a při psaní „Jan“ se jeho adresa nabídne vedle
skutečné. Adresa je v našeptávači vidět, takže riziko je malé, ale překlep v rychlosti je snadný.

**Kde:** `db.py` – `_learn_contacts`, `contacts()`.

**Oprava:** Z přijaté pošty se učit jen od odesílatelů, kterým jste už někdy psali, nebo jen ze zpráv, kde
`Authentication-Results` hlásí `dmarc=pass` (souvisí s issue 5). Případně kontakty jen z přijaté pošty
v našeptávači vizuálně odlišit.

---

## 5. Filtr spamu a pravidla věří hlavičce „Od“ (nízká)

**Problém:** Adresu odesílatele lze podvrhnout. Spammer, který se vydává za někoho, komu jste psali, obejde
bayesovský filtr (pojistka „bezpečných odesílatelů“). Stejně tak může podvrhnout zdroj notifikací.

**Kde:** `db.py` – `safe_senders()`; `sync.py` – `_apply_bayes()`.

**Oprava:** Za bezpečného považovat odesílatele jen tehdy, když hlavička `Authentication-Results` od vašeho
poskytovatele hlásí `dmarc=pass` (nebo `dkim=pass` pro doménu odesílatele).

---

## 6. Příkazy IMAP nekontrolují zalomení řádku (nízká)

**Problém:** Funkce `q()` dá do uvozovek i text se znaky `\r\n`, takže hledaný výraz nebo název složky mohou
do spojení vložit další příkaz IMAP. Týká se to jen vlastní schránky a vstupy zadává uživatel – jde o zpevnění.
Ověřeno 16. 11.: data z přijatých mailů se k `q()` se zalomením řádku nedostanou (adresy čistí `getaddresses`,
předmět `base_subject`).

**Kde:** `mail.py` – `q()`, `_search()`.

**Oprava:** V `q()` a ve vyhledávání odmítnout znaky `\r`, `\n` a `\0`.

---

## 7. Verze závislostí nejsou zafixované (nízká)

**Problém:** `requirements.txt` obsahuje `flask>=3.0` apod. Každé sestavení tak může přibalit jinou,
i problematickou verzi.

**Oprava:** Zafixovat přesné verze s kontrolními součty (`pip-compile --generate-hashes`) a aktualizovat vědomě.

---

## 8. V adresáři projektu zůstala stará data (nízká)

**Problém:** `C:\Dev\Mailer\data` je kopie z doby před přesunem dat do `%APPDATA%`. Obsahuje
`google_client.json` s Client Secret a starý `accounts.json` (bez hesel – ta jsou v klíčence). Při sdílení
projektu nebo nahrání do gitu by Client Secret unikl.

**Oprava:** Složku smazat (jen na pokyn uživatele).

---

## 9. Nešifrovaná data na disku (informativní)

**Problém:** `%APPDATA%\Mailer\mailer.db` obsahuje stažené zprávy nešifrovaně; chrání je jen účet Windows
(stejně jako u Thunderbirdu nebo Outlooku). Kdyby chyběla klíčenka Windows, hesla by se uložila jako čistý
text (teď neplatí – všechny účty mají heslo ve Správci pověření).

**Oprava:** Zapnout BitLocker. Případně v aplikaci výrazně varovat, když klíčenka chybí.

---

## 10. Celé zprávy v paměti (informativní)

**Problém:** Zprávy se načítají do paměti celé, takže zlomyslný server by mohl aplikaci zahltit obří zprávou.
Servery si ale uživatel přidává sám.

**Oprava:** Při stahování kontrolovat `RFC822.SIZE` a nad limit (např. 50 MB) zprávu stahovat po částech
nebo nabídnout jen přílohy ke stažení.
