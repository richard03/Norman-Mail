# Bezpečnostní issues

Nálezy z bezpečnostních auditů (7. 11. a 16. 11. 2026), seřazené od nejzávažnějších. Řešíme je jeden po druhém;
vyřešené issue se ze souboru smaže.

---

## 4. Přetažený odkaz přesměruje okno aplikace – cizí stránka pak má plný přístup k poště (střední)

**Problém:** Když do okna Maileru přetáhnete odkaz (třeba z prohlížeče nebo z jiného programu) mimo textové
pole, WebView2 na něj přesměruje celé okno. pywebview pak do každé načtené stránky vloží most
`window.pywebview.api` a nekontroluje, odkud volání přichází. Cizí stránka tak může zavolat
`api.request('GET', '/api/accounts')`, číst všechny zprávy, posílat maily vaším jménem nebo mazat.
Zkratka je potřeba akce uživatele (přetažení), ale dopad je úplný.
Přetažené *soubory* už jsou ošetřené (zachytávání `dragover`/`drop` v `app.js`), odkazy a text zatím ne.

**Kde:** `main.py` (nastavení WebView2); pywebview `edgechromium.py` – `on_navigation_completed` → `inject_pywebview`.

**Oprava:**
- V `main.py` se napojit na `CoreWebView2.NavigationStarting` a zrušit (`args.Cancel = True`) každou navigaci
  hlavního okna mimo `https://mailer-app.example/`. To je hlavní pojistka a pokryje i jiné cesty než přetažení.
- Navíc v `app.js` na úrovni dokumentu zachytit `dragover`/`drop` i pro `text/uri-list` a `text/plain`
  (mimo textová pole a okno zprávy), případně `AllowExternalDrop = false` u ovladače WebView2.
- V `Api.request` může pro jistotu ověřit, že okno je na adrese aplikace (`window.get_current_url()`).

---

## 11. Uložené přílohy nemají značku „stažené z internetu“ (střední)

**Problém:** `Api.save_attachment` zapisuje přílohu přes `Path.write_bytes` bez značky Mark of the Web
(alternativní datový proud `Zone.Identifier`). Windows pak u souboru z cizího mailu nevarují: SmartScreen
nekontroluje spustitelné soubory (.exe, .msi, .lnk, .js) a Office otevře dokument s makry bez chráněného
zobrazení. Outlook, Thunderbird i prohlížeče tuto značku přidávají; útoky přes přílohy na ni spoléhají.

**Kde:** `main.py` – `Api.save_attachment`.

**Oprava:** Po zápisu souboru zapsat `<cesta>:Zone.Identifier` s obsahem `[ZoneTransfer]
ZoneId=3
`
(chybu na disku bez NTFS, třeba FAT na flashce, ignorovat). Totéž u ukládání přes WebView2
(`ALLOW_DOWNLOADS`), pokud se ještě někde používá.

---

## 12. Název přílohy může maskovat svou příponu (nízká)

**Problém:** Název přílohy se zobrazuje i nabízí k uložení tak, jak přišel. Neviditelný znak pro obrácení
směru textu (U+202E) změní zobrazení: `faktura‮fdp.exe` se ukáže jako „fakturaexe.pdf“.
Podobně fungují dlouhé mezery před `.exe`.

**Kde:** `mail.py` – `parse_message`, `attachments_from_raw` (název přílohy); zobrazení v `app.js`.

**Oprava:** Z názvů příloh odstranit řídicí znaky směru a další neviditelné znaky
(U+200B–U+200F, U+202A–U+202E, U+2066–U+2069, řídicí znaky C0/C1), shluky mezer zkrátit na jednu.
U spustitelných přípon (.exe, .scr, .js, .lnk, .bat, .cmd, .msi, .vbs, .ps1…) zobrazit u přílohy varování.

---

## 13. Odkaz ve zprávě může načíst cizí stránku přímo do čtecího rámce (nízká)

**Problém:** Odkazy ve zprávě se mají otevírat v prohlížeči (`<base target="_blank">`), ale atribut `target`
přímo na odkazu má přednost. Odkaz s `target="_self"` (nebo `<area>`, pojmenovaný cíl) po kliknutí načte cizí
stránku do rámce zprávy uvnitř aplikace. Skripty a formuláře tam neběží (sandbox), ale stránka se může tvářit
jako část Maileru (falešná hláška „přihlaste se znovu“ s odkazem) a načte se z cizího serveru.

**Kde:** `static/app.js` – `buildFrameDoc`.

**Oprava:** V `buildFrameDoc` u všech `a` a `area` nastavit `target="_blank"` (přepsat původní hodnotu).
Navíc v `main.py` rušit navigace rámců (`CoreWebView2.FrameNavigationStarting`) jinam než na `about:srcdoc`.
Hodí se to udělat spolu s issue 4.

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
