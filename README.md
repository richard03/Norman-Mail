# Mailer

Jednoduchý desktopový emailový klient pro Windows, který obslouží více účtů najednou (IMAP + SMTP).

## Spuštění

**Sestavená aplikace:** spusťte `dist\Mailer\Mailer.exe`. Složka `dist\Mailer` obsahuje vše potřebné, takže ji můžete přesunout kamkoli (např. do `C:\Program Files\Mailer`). Zástupce na plochu vytvoříte pravým tlačítkem na `Mailer.exe` → *Zobrazit další možnosti* → *Odeslat* → *Plocha*.

**Sestavení `.exe`:** spusťte `build.bat`. Nainstaluje závislosti a PyInstaller a výsledek uloží do `dist\Mailer\`.

**Spuštění ze zdrojových kódů** (pro vývoj): `start.bat`, nebo `.venv\Scripts\python main.py`.

**Webový režim** (rozhraní v prohlížeči): `.venv\Scripts\python app.py` otevře <http://127.0.0.1:8765/>.

### Jak to funguje

`main.py` spustí na pozadí lokální server (Flask, náhodný volný port na `127.0.0.1`) a zobrazí jeho rozhraní v nativním okně přes **Edge WebView2** (knihovna pywebview). WebView2 je součástí Windows 10 a 11. Odkazy z e-mailů a přihlášení přes Google se otevírají ve vašem výchozím prohlížeči. Povolené jsou jen odkazy `http`, `https` a `mailto`. Přílohy se ukládají přes systémový dialog *Uložit jako*. Vestavěná ochrana WebView2 před sledováním je vypnutá, protože blokovala obrázky v newsletterech i po kliknutí na *Zobrazit obrázky*. Vzdálený obsah zpráv blokuje sama aplikace, dokud ho nepovolíte.

### Lokální databáze (cache)

Mailer si drží vlastní databázi metadat zpráv v souboru `%APPDATA%\Mailer\mailer.db` (SQLite):

- **Seznamy zpráv se zobrazují okamžitě z databáze.** Synchronizace na pozadí (vlastní IMAP spojení, každou minutu a po každé akci) ze serveru jen doplňuje rozdíly: nové zprávy, smazané zprávy a změny příznaků provedené v jiných klientech. Nejdřív se synchronizuje doručená pošta všech účtů, potom ostatní složky. Historie velkých složek se dohrává po dávkách od nejnovějších zpráv.
- **Otevřené zprávy** (včetně příloh) se uloží celé, takže se podruhé otevřou bez stahování a jdou přečíst i offline. Výjimkou jsou zprávy větší než 25 MB.
- **Kontext** (související zprávy) se po dosynchronizování složek počítá přímo z databáze. Do té doby se dohledává na serveru.
- **Odložené zprávy:** akce *Odložit o den* / *Odložit o týden* označí zprávu jako přečtenou a zítra, resp. za týden v 8:00, ji zase označí jako nepřečtenou. Pokud aplikace v tu chvíli neběží, stane se to hned po dalším spuštění.

- **Pravidla pro spam:** označením zprávy jako *Spam* vznikne pravidlo „odesílatel je spam“. Nové zprávy od tohoto odesílatele synchronizace ve všech účtech přesune do spamu dřív, než se objeví v seznamu. Pravidla jsou přehledně v **Nastavení** (dole v levém sloupci), kde je lze mazat. Pro vlastní adresy pravidla nevznikají.
- **Vzít na vědomí:** zprávy z doručené pošty, ve kterých nejste v poli Komu, tedy jste v kopii nebo ve skryté kopii. Tyto zprávy se nezobrazují v **Důležité**. Notifikace mají přednost.
- **Důležité / Notifikace:** tlačítko *Notifikace* u zprávy označí jejího odesílatele za zdroj notifikací. Všechny jeho dosavadní i budoucí zprávy se pak zobrazují ve složce **Notifikace** a ne v **Důležité** (výchozí pohled po startu). **Všechny doručené** ukazuje obojí. Zprávy zůstávají na serveru v doručené poště, jde jen o lokální třídění. U zprávy, která už notifikací je, se tlačítko změní na *Není notifikace*. Pravidla lze mazat v **Nastavení**.
- **Nastavení notifikací** (v Nastavení pod pravidly):
  - *Když opustím sekci Notifikace, označit všechny notifikace jako přečtené* (výchozí: vypnuto). Uplatní se při přepnutí na jiný pohled.
  - *Mazat přečtené notifikace starší než týden* (výchozí: zapnuto).
  - *Mazat nepřečtené notifikace starší než týden* (výchozí: vypnuto).

  Mazání probíhá v rámci synchronizace (přesun do koše). Odložené zprávy se nemažou.
- **Složky** (levý sloupec, *+ Přidat složku*): každá složka v aplikaci míří na **jednu konkrétní složku na jednom účtu**, buď existující na serveru, nebo novou. V konfiguraci (⚙ u složky) nastavíte cílový účet a složku, pravidla (e-mail odesílatele, lze přidat a smazat), kdy přesouvat (*ihned po doručení* / *až po přečtení*) a zda přesunuté zprávy označit jako přečtené. Pravidlo vznikne i tlačítkem **Do složky…** ve sloupci Akce.
  - Typy pravidel: **odesílatel**, **příjemce** (adresa v Komu nebo Kopii, např. když jste v kopii a příjemcem je kolega) a **slovo či fráze v předmětu** (bez ohledu na velikost písmen a předpony Re:/Fw:).
  - Pravidla platí pro doručenou poštu všech účtů. Zprávy z jiného účtu se přenesou na cílový účet (s původním datem a příznaky) a na původním účtu skončí v **koši**. Když uložení na cílový server selže, ze zdroje se nic nemaže.
  - Odložené zprávy se nepřesouvají. Odesílatel může patřit jen do jedné složky. *Smazat složku* nabídne volbu: **jen v aplikaci** (složka i zprávy na serveru zůstanou), nebo **i na serveru včetně všech zpráv** (zprávy se přesunou do koše účtu, pak se složka smaže, potvrzuje se druhým kliknutím).
  - Složky z dřívější verze bez cílového účtu se nepoužívají, dokud jim účet nenastavíte (v levém sloupci mají červený vykřičník).

Databázi lze kdykoli smazat (při zavřené aplikaci). Při dalším spuštění se znovu stáhne ze serverů. Přijdete jen o odložení, pravidla pro spam, notifikace a složky a o konfiguraci složek (samotné složky a zprávy na serveru zůstanou).

### Kde jsou data

Nastavení účtů je uložené v `%APPDATA%\Mailer\`, hesla a tokeny ve Správci pověření Windows. Nastavení ze starší verze (složka `data\` vedle zdrojových kódů) se při prvním spuštění zkopíruje automaticky.

## Co umí

- Libovolný počet účtů a společnou složku **Všechny doručené**, která slučuje doručenou poštu ze všech účtů (účty se načítají paralelně a jsou barevně odlišené).
- Procházení všech složek každého účtu s počtem nepřečtených zpráv.
- Čtení HTML i textových zpráv a stahování příloh. Vložené obrázky (`cid:`) se zobrazí přímo ve zprávě.
- Psaní nových zpráv, odpovědi, odpovědi všem a přeposílání včetně původních příloh. Lze přidat kopii, skrytou kopii i vlastní přílohy.
- Odesílání z kteréhokoli účtu. Kopie odeslané zprávy se uloží do složky Odeslané.
- Mazání zpráv (přesun do koše), označení zprávy jako nepřečtené a označení hvězdičkou.
- Vyhledávání ve složce (IMAP SEARCH).
- Automatická kontrola nové pošty každé 2 minuty.
- Předvolby serverů pro Gmail, Seznam, Centrum, iCloud, Yahoo a Outlook.

## Bezpečnost

- Hesla se ukládají do **Správce pověření Windows** (přes knihovnu `keyring`), ne do souboru. Soubor `accounts.json` obsahuje jen nastavení serverů.
- Lokální server naslouchá pouze na `127.0.0.1` a odmítá cizí hlavičky Host a Origin (ochrana proti CSRF a DNS rebindingu).
- HTML zprávy se zobrazují v izolovaném rámci (iframe) s atributem sandbox, takže se v nich nespustí žádné skripty. Vzdálené obrázky jsou ve výchozím stavu zablokované, protože slouží ke sledování otevření zprávy. Lze je povolit tlačítkem.
- Přílohy se vždy stahují jako soubor, nikdy se neotevírají v rámci aplikace.

## Gmail – přihlášení přes Google (OAuth)

Gmail se dá připojit bez hesla tlačítkem **Přihlásit přes Google**. Aplikace k tomu potřebuje vlastního *OAuth klienta*. Jednorázově ho vytvoříte takto (asi 10 minut):

1. Otevřete <https://console.cloud.google.com/> a vytvořte nový projekt (např. „Mailer“).
2. **APIs & Services → Library**: vyhledejte **Gmail API** a klikněte na **Enable**.
3. **APIs & Services → OAuth consent screen** (v novějším rozhraní *Google Auth Platform*):
   - User type: **External**, vyplňte název aplikace a svůj email.
   - V části **Audience / Test users** přidejte svou Gmail adresu (i všechny další adresy, které chcete připojit).
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type: **Desktop app**. Redirect URI se nezadává, protože aplikace používá `http://127.0.0.1:<port>/oauth/callback`, který Google u desktopových klientů povoluje automaticky.
   - Zkopírujte **Client ID** a **Client Secret**.
5. V Maileru klikněte na **+ Přidat účet**, zvolte Gmail a **Přihlásit přes Google**. Při prvním použití vložte Client ID a Client Secret. Uloží se do `%APPDATA%\Mailer\google_client.json`.
6. V okně Googlu se přihlaste a povolte přístup. Google zobrazí varování „Google tuto aplikaci neověřil“. Je to v pořádku, protože jde o vaši vlastní aplikaci: klikněte na **Pokračovat**.

Obnovovací token se uloží do Správce pověření Windows. Krátkodobé přístupové tokeny si aplikace obnovuje sama.

> **Důležité:** dokud je aplikace v Google Cloud ve stavu **Testing**, platnost přihlášení vyprší po 7 dnech a bude potřeba se přihlásit znovu (Nastavení účtu → Přihlásit znovu). Tomu se vyhnete, když na stránce OAuth consent screen / Audience kliknete na **Publish app** (stav „In production“). Pro osobní použití není nutné ověření aplikace u Googlu, jen zůstane zobrazené varování o neověřené aplikaci.

Přihlášení můžete kdykoli zrušit na <https://myaccount.google.com/permissions>.

## Poznámky k poskytovatelům

- **Gmail (alternativa bez OAuth), iCloud a Yahoo:** místo běžného hesla je potřeba vytvořit *heslo aplikace*.
- **Seznam:** v nastavení schránky musí být povolený přístup přes IMAP.
- **Outlook.com / Microsoft 365:** Microsoft u většiny účtů zakázal přihlašování heslem a vyžaduje OAuth, které tento jednoduchý klient nepodporuje.

## Struktura

| Soubor | Účel |
|---|---|
| `main.py` | Desktopová aplikace (okno, ukládání příloh) |
| `app.py` | Lokální server (Flask) a REST API |
| `google_oauth.py` | Přihlášení přes Google (OAuth) |
| `db.py` | Lokální databáze metadat (SQLite) |
| `sync.py` | Synchronizace se servery na pozadí, probouzení odložených zpráv |
| `mail.py` | Práce s IMAP a SMTP, parsování zpráv, sdílení spojení |
| `store.py` | Ukládání účtů a hesel |
| `static/` | Uživatelské rozhraní (HTML, CSS, JS) |

Port lze změnit proměnnou prostředí `MAILER_PORT`, složku s daty proměnnou `MAILER_DATA`.
