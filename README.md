# Mailer

Jednoduchý desktopový emailový klient pro Windows, který obslouží více účtů najednou (IMAP + SMTP).

## Spuštění

**Sestavená aplikace:** spusťte `dist\Mailer\Mailer.exe`. Složka `dist\Mailer` obsahuje vše potřebné, takže ji můžete přesunout kamkoli (např. do `C:\Program Files\Mailer`). Zástupce na plochu vytvoříte pravým tlačítkem na `Mailer.exe` → *Zobrazit další možnosti* → *Odeslat* → *Plocha*.

**Připnutí na hlavní panel:** na běžícím Maileru klikněte pravým tlačítkem na ikonu na hlavním panelu → *Připnout na hlavní panel*. Připnutá ikona spustí tu verzi, ze které jste připínali: `Mailer.exe`, nebo verzi ze zdrojových kódů (`start.bat`). Okno má vlastní identitu aplikace, takže se už nepřipne Python.

**Sestavení `.exe`:** spusťte `build.bat`. Nainstaluje závislosti a PyInstaller a výsledek uloží do `dist\Mailer\`.

**Spuštění ze zdrojových kódů** (pro vývoj): `start.bat`, nebo `.venv\Scripts\python main.py`.

**Vývojový server** (rozhraní v prohlížeči, jen pro ladění): `.venv\Scripts\python tools\dev_server.py` otevře <http://127.0.0.1:8765/>. Není součástí `Mailer.exe`. Dokud běží, může se k němu připojit kterýkoli program na počítači, proto ho nepouštějte mimo vývoj.

### Jak to funguje

`main.py` zobrazí rozhraní v nativním okně přes **Edge WebView2** (knihovna pywebview). **Aplikace neotevírá žádný síťový port:** stránka se načítá přímo ze souborů (virtuální doména WebView2 `https://mailer-app.example/`) a rozhraní volá Python přes most pywebview uvnitř procesu. Jiný program ani jiný uživatel počítače se k ní tedy nepřipojí. Jedinou výjimkou je přihlášení přes Google: na dobu přihlášení se na `127.0.0.1` spustí dočasný server, který přijímá jen návrat z prohlížeče (`/oauth/callback`) a hned potom se vypne. WebView2 je součástí Windows 10 a 11. Odkazy z e-mailů a přihlášení přes Google se otevírají ve vašem výchozím prohlížeči. Povolené jsou jen odkazy `http`, `https` a `mailto`. Přílohy se ukládají přes systémový dialog *Uložit jako*. Vestavěná ochrana WebView2 před sledováním je vypnutá, protože blokovala obrázky v newsletterech i po kliknutí na *Zobrazit obrázky*. Vzdálený obsah zpráv blokuje sama aplikace, dokud ho nepovolíte. HTML zprávy se před zobrazením vyčistí stejným parserem, který ho vykreslí: odstraní se značky `link`, `meta`, `base`, `script`, `iframe`, `object` apod., formuláře a atributy `on…` a `ping`, a v rámci zprávy jsou vypnuté předběžné dotazy DNS. Zpráva tak nemůže prozradit, že jste ji otevřeli, ani přes DNS nebo přesměrování.

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
- **Složky** (levý sloupec, *+ Přidat složku*): každá složka v aplikaci míří na **jednu konkrétní složku na jednom účtu**, buď existující na serveru, nebo novou. Konfigurace složek je v **Nastavení** pod Spamem a Notifikacemi, v sekci **Složky**. Každá složka tam má svou obrazovku: uprostřed je konfigurace, vpravo pravidla. Je tam i *+ Nová složka*. Tlačítko ⚙ u složky v levém sloupci otevře Nastavení rovnou na obrazovce této složky. V konfiguraci nastavíte cílový účet a složku, pravidla (e-mail odesílatele, lze přidat a smazat), kdy přesouvat (*ihned po doručení* / *až po přečtení*) a zda přesunuté zprávy označit jako přečtené. Pravidlo vznikne i tlačítkem **Do složky…** ve sloupci Akce.
  - Typy pravidel: **odesílatel**, **příjemce** (adresa v Komu nebo Kopii, např. když jste v kopii a příjemcem je kolega) a **slovo či fráze v předmětu** (bez ohledu na velikost písmen a předpony Re:/Fw:).
  - Pravidla platí pro doručenou poštu všech účtů. Zprávy z jiného účtu se přenesou na cílový účet (s původním datem a příznaky) a na původním účtu skončí v **koši**. Když uložení na cílový server selže, ze zdroje se nic nemaže.
  - Odložené zprávy se nepřesouvají. Odesílatel může patřit jen do jedné složky. *Smazat složku* nabídne volbu: **jen v aplikaci** (složka i zprávy na serveru zůstanou), nebo **i na serveru včetně všech zpráv** (zprávy se přesunou do koše účtu, pak se složka smaže, potvrzuje se druhým kliknutím).
  - Složky z dřívější verze bez cílového účtu se nepoužívají, dokud jim účet nenastavíte (v levém sloupci mají červený vykřičník).

- **Smazané** (levý sloupec): smazané zprávy ze všech účtů na jednom místě (koše jednotlivých účtů). Zprávy v této složce se automaticky označí jako přečtené, i když je tam přesunul jiný klient. Číslo u složky ukazuje celkový počet zpráv. Tlačítko **Vyprázdnit** dole pod seznamem je po potvrzení natrvalo smaže ze všech serverů. Smazání zprávy přímo ve Smazaných je definitivní a potvrzuje se.
- **Spam** (levý sloupec): spam ze všech účtů na jednom místě, aby šlo najít, co tam spadlo omylem. Číslo u složky ukazuje nepřečtené zprávy. Ve Spamu se z tlačítka Spam stane **Není spam**: zpráva se vrátí do doručené pošty svého účtu a pravidlo spamu pro jejího odesílatele se zruší, aby další zprávy už do spamu nepadaly. Pravidlo se zruší i při přetažení zprávy ze Spamu do doručené pošty nebo do složky.
- **Bayesovský filtr spamu** (Nastavení → Spam): pozná spam podle obsahu i od odesílatelů, kteří ještě nejsou v pravidlech. Učí se z toho, co označíte: tlačítko Spam nebo přetažení do Spamu = spam, **Není spam** = dobrá zpráva. Dobré zprávy se učí i samy: posouzená zpráva, kterou přečtete a necháte 3 dny v doručené poště, se naučí jako dobrá. Napoprvé se filtr naučí z existujících zpráv (spam = zprávy ve spamu od odesílatelů z pravidel, dobré = přečtené zprávy v doručené poště).
  - Posuzuje jen nové nepřečtené zprávy (z posledních 3 dnů). Nikdy nepřesune zprávy od lidí, kterým jste psali, od zdrojů notifikací ani zprávy odpovídající pravidlům složek.
  - Spam začne přesouvat, až zná aspoň 10 spamových a 10 dobrých zpráv. Citlivost: vysoká (hranice 80 %), střední (90 %), nízká (97 %). Filtr jde vypnout; „Zapomenout a naučit znovu“ ho vrátí do výchozího stavu.
  - U zprávy, kterou filtr přesunul do spamu, je v záhlaví štítek „Filtr spamu: 99 %“; po najetí myší ukáže slova, podle kterých rozhodl.
  - Metoda: Robinsonova/Fisherova kombinace pravděpodobností slov (jako SpamBayes), slova z předmětu, odesílatele, vybraných hlaviček, odkazů a prvních 32 kB zprávy. Všechno se počítá a ukládá lokálně v databázi aplikace.
- **Výběr více zpráv:** Ctrl+klik přidá zprávu do výběru nebo ji z něj odebere, Shift+klik vybere rozsah, Ctrl+A vybere všechny načtené zprávy. Vybrané zprávy smažete klávesou **Delete** nebo tlačítkem Smazat, tlačítkem Spam (ve Spamu Není spam) je přesunete hromadně.
- **Drag & drop:** zprávu nebo vybranou skupinu zpráv přetáhněte myší na položku v levém sloupci:
  - na **složku**: přesun do ní (i na jiný účet), pravidlo se nevytváří;
  - na **Smazané**: smazání;
  - na **Spam**: přesun do spamu, odesílatelé se přidají do pravidel spamu;
  - na **účet**: přesun do doručené pošty tohoto účtu;
  - na **Notifikace**: odesílatelé se stanou zdrojem notifikací;
  - na **Důležité** nebo **Vzít na vědomí**: odesílatelé přestanou být notifikacemi;
  - ze Smazaných nebo ze složky na kterýkoli pohled doručené pošty: obnovení do doručené pošty.

  Pokud by zprávy po přesunu hned zmizely, aplikace se předem zeptá. Jde o tyto případy: staré notifikace by se podle nastavení smazaly, odesílatel je označený jako spam, nebo zprávu hned přesune pravidlo složky.

- **Kontextové menu** (pravé tlačítko myši na položce levého panelu):
  - **Označit vše jako přečtené**: platí pro pohledy doručené pošty, složky i účty. V aplikaci se změna projeví hned, server ji dostane na pozadí.
  - **Nastavení**: u složky otevře její obrazovku v Nastavení, u účtu nastavení účtu, u Notifikací nastavení notifikací.
  - Položky, které u dané složky nedávají smysl, jsou zašedlé (např. u Smazaných).

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
- Uložené heslo se posílá jen na server a pod jménem, pro které bylo zadané. Když v nastavení účtu změníte server, port, zabezpečení nebo uživatelské jméno, je potřeba heslo zadat znovu. U účtů přihlášených přes Google jsou servery a adresa pevně dané (Gmail) a z formuláře se měnit nedají.
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
| `app.py` | Jádro aplikace (Flask endpointy `/api/*`), volané přes most pywebview |
| `tools/dev_server.py` | Vývojový server pro ladění v prohlížeči (není v `Mailer.exe`) |
| `google_oauth.py` | Přihlášení přes Google (OAuth) |
| `db.py` | Lokální databáze metadat (SQLite) |
| `sync.py` | Synchronizace se servery na pozadí, probouzení odložených zpráv |
| `mail.py` | Práce s IMAP a SMTP, parsování zpráv, sdílení spojení |
| `store.py` | Ukládání účtů a hesel |
| `static/` | Uživatelské rozhraní (HTML, CSS, JS) |

Port lze změnit proměnnou prostředí `MAILER_PORT`, složku s daty proměnnou `MAILER_DATA`.
