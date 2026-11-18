# Norman Mail

Jednoduchý desktopový emailový klient pro Windows, který obslouží více účtů najednou (IMAP + SMTP).

## Spuštění

**Pro Windows bez Pythonu** (Windows 10/11, 64bit): celá aplikace je jediný soubor `NormanMail.exe` (asi 12 MB, obsahuje vše potřebné včetně Pythonu). Nic se neinstaluje: uložte ho kamkoli a spusťte. Zástupce v nabídce Start nebo na ploše vytvoříte v **Nastavení → O aplikaci**. Data jsou v `%APPDATA%\Mailer`, hesla ve Správci pověření Windows. Aplikaci odstraníte smazáním souboru (a zástupců).
- **Portable režim:** když vedle `NormanMail.exe` položíte soubor `portable.txt` (je v `assets\`), ukládá aplikace všechna data do složky `data` vedle sebe, např. na flash disku. Hesla jsou z bezpečnostních důvodů ve Správci pověření Windows daného počítače, takže na jiném počítači je zadáte znovu.
- Při každém spuštění se aplikace nejdřív rozbalí do dočasné složky (`%TEMP%\_MEI…`), což zabere asi 2 s. Úplně první spuštění trvá déle (kontrola antivirem, příprava WebView2). Dočasné složky, které zůstanou po pádu, aplikace při dalším spuštění sama uklidí.
- Běží vždy jen jedna instance. Druhé spuštění jen přenese okno běžící aplikace do popředí.
- **Automatické aktualizace:** jednou denně se aplikace podívá do [GitHub Releases](https://github.com/richard03/Norman-Mail/releases), stáhne novou verzi na pozadí vedle sebe (`NormanMail.exe.new`) a nainstaluje ji po kliknutí na *Aktualizovat na X.Y.Z* v levém panelu, nebo sama při zavření aplikace. Vypnout jde v **Nastavení → O aplikaci**, tam je i tlačítko *Zkontrolovat teď*. Když do složky s exe nejde zapisovat (třeba `C:\Program Files`), aplikace jen nabídne stránku ke stažení. Na GitHub se neposílá nic o uživateli, jen běžný požadavek s verzí aplikace.

Jedinou systémovou součástí, kterou Norman Mail potřebuje, je **Microsoft Edge WebView2 Runtime**. Windows 11 ho mají vždy, Windows 10 téměř vždy (instaluje se s Edgem). Pokud chybí, aplikace nabídne stránku Microsoftu ke stažení.

**Připnutí na hlavní panel:** na běžící aplikaci klikněte pravým tlačítkem na ikonu na hlavním panelu → *Připnout na hlavní panel*. Připnutá ikona spustí tu verzi, ze které jste připínali: `NormanMail.exe`, nebo verzi ze zdrojových kódů (`start.bat`). Okno má vlastní identitu aplikace, takže se už nepřipne Python.

**Sestavení:** spusťte `build.bat`. Nainstaluje závislosti a PyInstaller a podle `NormanMail.spec` sestaví `dist\NormanMail.exe`. Verze je v `version.py` (propisuje se i do vlastností exe). Spec vynechává nepotřebné moduly standardní knihovny a kompiluje s `optimize=2`, proto se v kódu nesmí používat `assert` jako kontrola za běhu. Pro ladění sestavte verzi s konzolí: `set NORMANMAIL_CONSOLE=1` a pak `build.bat`.

**Vydání nové verze:** zvyšte `VERSION` ve `version.py`, commitněte a pushněte a spusťte `.venv\Scripts\python tools\release.py --notes "Co je nového"`. Skript zkontroluje čistý strom a novou verzi, sestaví exe, vytvoří a podepíše `latest.json` a přes [GitHub CLI](https://cli.github.com/) (`gh`, `winget install GitHub.cli`, `gh auth login`) založí release `vX.Y.Z`. Bez `gh` vypíše tři soubory k ručnímu nahrání. Podepisuje se soukromým klíčem z `%USERPROFILE%\.normanmail\release_key`, který vznikl jednou přes `tools\release.py --genkey` a nesmí se dostat do repozitáře. **Klíč zálohujte:** bez něj už nainstalované verze žádnou další aktualizaci nepřijmou a uživatelé by si novou verzi museli stáhnout ručně. Aktualizace jde vyzkoušet z místní složky: `set NORMANMAIL_UPDATE_URL=http://127.0.0.1:8000` (složka s `latest.json`, `latest.json.sig` a `NormanMail.exe`, třeba `python -m http.server` v `dist`), podpis se ověřuje i tak.

**Spuštění ze zdrojových kódů** (pro vývoj): `start.bat`, nebo `.venv\Scripts\python main.py`.

**Vývojový server** (rozhraní v prohlížeči, jen pro ladění): `.venv\Scripts\python tools\dev_server.py` otevře <http://127.0.0.1:8765/>. Není součástí `NormanMail.exe`. Dokud běží, může se k němu připojit kterýkoli program na počítači, proto ho nepouštějte mimo vývoj.

### Jak to funguje

`main.py` zobrazí rozhraní v nativním okně přes **Edge WebView2** (knihovna pywebview). **Aplikace neotevírá žádný síťový port:** stránka se načítá přímo ze souborů (virtuální doména WebView2 `https://mailer-app.example/`) a rozhraní volá Python přes most pywebview uvnitř procesu. Jiný program ani jiný uživatel počítače se k ní tedy nepřipojí. Jedinou výjimkou je přihlášení přes Google: na dobu přihlášení se na `127.0.0.1` spustí dočasný server, který přijímá jen návrat z prohlížeče (`/oauth/callback`) a hned potom se vypne. WebView2 je součástí Windows 10 a 11. Odkazy z e-mailů a přihlášení přes Google se otevírají ve vašem výchozím prohlížeči. Povolené jsou jen odkazy `http`, `https` a `mailto`. Přílohy se ukládají přes systémový dialog *Uložit jako*. Vestavěná ochrana WebView2 před sledováním je vypnutá, protože blokovala obrázky v newsletterech i po kliknutí na *Zobrazit obrázky*. Vzdálený obsah zpráv blokuje sama aplikace, dokud ho nepovolíte. HTML zprávy se před zobrazením vyčistí stejným parserem, který ho vykreslí: odstraní se značky `link`, `meta`, `base`, `script`, `iframe`, `object` apod., formuláře a atributy `on…` a `ping`, a v rámci zprávy jsou vypnuté předběžné dotazy DNS. Zpráva tak nemůže prozradit, že jste ji otevřeli, ani přes DNS nebo přesměrování.

**Tmavý režim:** aplikace se řídí světlým / tmavým režimem Windows, včetně HTML zpráv. V tmavém režimu se zpráva, která má vlastní tmavou verzi (`prefers-color-scheme`), zobrazí v ní. Ostatní zprávy se barevně převrátí a obrázky se vrátí do původních barev (podobně jako v Gmailu nebo Outlooku). Když převrácení nesedí, odkaz *Zobrazit v původních barvách* v záhlaví zprávy ukáže originál. Přepnutí režimu Windows za běhu se projeví hned.

### Lokální databáze (cache)

Norman Mail si drží vlastní databázi metadat zpráv v souboru `%APPDATA%\Mailer\mailer.db` (SQLite):

- **Seznamy zpráv se zobrazují okamžitě z databáze.** Synchronizace na pozadí (vlastní IMAP spojení, každou minutu a po každé akci) ze serveru jen doplňuje rozdíly: nové zprávy, smazané zprávy a změny příznaků provedené v jiných klientech. Nejdřív se synchronizuje doručená pošta všech účtů, potom ostatní složky. Historie velkých složek se dohrává po dávkách od nejnovějších zpráv.
- **Otevřené zprávy** (včetně příloh) se uloží celé, takže se podruhé otevřou bez stahování a jdou přečíst i offline. Výjimkou jsou zprávy větší než 25 MB.
- **Kontext** (související zprávy) se po dosynchronizování složek počítá přímo z databáze. Do té doby se dohledává na serveru.
  Kliknutím na zprávu v kontextu se zpráva otevře a zároveň se nalistuje v seznamu vlevo. Aplikace ji hledá v tomto pořadí: složka aplikace → Důležité / Vzít na vědomí / Nedůležité / Notifikace → Spam / Smazané. Zprávu z ostatních složek (Odeslané, Archiv…) ukáže v dané složce účtu.
- **Odložené zprávy:** akce *Odložit o den* / *Odložit o týden* označí zprávu jako přečtenou a zítra, resp. za týden v 8:00, ji zase označí jako nepřečtenou. Pokud aplikace v tu chvíli neběží, stane se to hned po dalším spuštění.

- **Pravidla pro spam:** označením zprávy jako *Spam* vznikne pravidlo „odesílatel je spam“. Nové zprávy od tohoto odesílatele synchronizace ve všech účtech přesune do spamu dřív, než se objeví v seznamu. Pravidla jsou přehledně v **Nastavení** (dole v levém sloupci), kde je lze mazat. Pro vlastní adresy pravidla nevznikají.
- **Vzít na vědomí:** zprávy z doručené pošty, ve kterých nejste v poli Komu, tedy jste v kopii nebo ve skryté kopii. Tyto zprávy se nezobrazují v **Důležité**. Nedůležité a Notifikace mají přednost.
- **Nedůležité / Notifikace:** tlačítka *Nedůležité* a *Notifikace* (sloupec s tlačítky, sekce *Třídění*) nabídnou, co zařadit:
  - *Jen tento email*: zařadí jen tuto zprávu, pravidlo nevzniká;
  - *Vše od tohoto odesílatele*: pravidlo pro všechny dosavadní i budoucí zprávy od odesílatele;
  - *Vše s tímto subjektem*: pravidlo pro všechny zprávy, jejichž předmět obsahuje předmět této zprávy (bez předpon Re:/Fw:, bez ohledu na velikost písmen).

  Zprávy se pak zobrazují ve složce **Nedůležité**, resp. **Notifikace**, a ne v **Důležité** (výchozí pohled po startu). Každá zpráva je v levém sloupci vidět právě v jednom seznamu, souhrnné pohledy typu „Všechny doručené“ záměrně nejsou. Zprávy zůstávají na serveru v doručené poště, jde jen o lokální třídění. Zařazení jedné zprávy má přednost před pravidly; když zprávě odpovídají pravidla obou složek, vyhrají Notifikace. Stejné pravidlo (odesílatel nebo předmět) může být v obou složkách – zpráva pak patří do Notifikací, a když jedno z pravidel smažete, druhé platí dál. U zprávy, která už nedůležitá / notifikací je, se tlačítko změní na *Není nedůležité* / *Není notifikace*: zpráva se vrátí do Důležité a zruší se i pravidla, podle kterých se tam řadila. Pravidla lze mazat v **Nastavení** (sekce Nedůležité je první, Notifikace pod Spamem).
- **Nastavení Nedůležitých a Notifikací** (každá složka zvlášť):
  - *Když opustím sekci …, označit všechny zprávy jako přečtené* (výchozí: vypnuto). Uplatní se při přepnutí na jiný pohled.
  - *Mazat přečtené zprávy starší než týden* / *starší než měsíc*.
  - *Mazat nepřečtené zprávy starší než týden* / *starší než měsíc*.

  Všechny mazací volby jsou ve výchozím stavu vypnuté. Když je zaškrtnutý týden i měsíc, platí kratší lhůta.

  Mazání probíhá v rámci synchronizace (přesun do koše). Odložené zprávy se nemažou.
- **Složky** (levý sloupec, *+ Přidat složku*): každá složka v aplikaci míří na **jednu konkrétní složku na jednom účtu**, buď existující na serveru, nebo novou. Konfigurace složek je v **Nastavení** pod Nedůležitými, Notifikacemi, Spamem a Smazanými, v sekci **Složky**. Každá složka tam má svou obrazovku: uprostřed je konfigurace, vpravo pravidla. Je tam i *+ Nová složka*. Tlačítko ⚙ u složky v levém sloupci otevře Nastavení rovnou na obrazovce této složky. V konfiguraci nastavíte cílový účet a složku a zda zprávy přesunuté ihned po doručení označit jako přečtené. Vpravo jsou pravidla (odesílatel, příjemce nebo část předmětu, lze přidat a smazat). Každé pravidlo má vlastní nastavení, kdy přesouvat: *ihned po doručení* nebo *až po přečtení*.
  - **Nastavit pravidlo…** (sloupec s tlačítky, sekce *Třídění*): otevře okno, kde u zprávy zvolíte složku, kdy přesouvat a podle čeho poznat další zprávy (odesílatel, příjemce, část předmětu). Zpráva se do složky přesune hned a pravidlo platí pro další zprávy.
  - Typy pravidel: **odesílatel**, **příjemce** (adresa v Komu nebo Kopii, např. když jste v kopii a příjemcem je kolega) a **slovo či fráze v předmětu** (bez ohledu na velikost písmen a předpony Re:/Fw:).
  - Pravidla platí pro doručenou poštu všech účtů. Zprávy z jiného účtu se přenesou na cílový účet (s původním datem a příznaky) a na původním účtu skončí v **koši**. Když uložení na cílový server selže, ze zdroje se nic nemaže.
  - Odložené zprávy se nepřesouvají. Odesílatel může patřit jen do jedné složky. *Smazat složku* nabídne volbu: **jen v aplikaci** (složka i zprávy na serveru zůstanou), nebo **i na serveru včetně všech zpráv** (zprávy se přesunou do koše účtu, pak se složka smaže, potvrzuje se druhým kliknutím).
  - Složky z dřívější verze bez cílového účtu se nepoužívají, dokud jim účet nenastavíte (v levém sloupci mají červený vykřičník).

- **Úprava pravidel:** v Nastavení má každé pravidlo tlačítko *Upravit*.
  - U Nedůležitých a Notifikací se otevře okno se třemi poli: *Odesílatel*, *Příjemce* (adresa v Komu nebo Kopii) a *Část předmětu*. Zpráva musí splnit všechna vyplněná pole, prázdné pole se nekontroluje.
  - U Spamu a složek se pravidlo upravuje přímo v řádku (typ a hodnota, u složek i kdy přesouvat). *Uložit* (Enter) změnu uloží, *Zrušit* (Esc) ji zahodí.
  - Stejné pravidlo nejde v jednom seznamu mít dvakrát.
- **Smazané** (levý sloupec): smazané zprávy ze všech účtů na jednom místě (koše jednotlivých účtů). Zprávy v této složce se automaticky označí jako přečtené, i když je tam přesunul jiný klient. Číslo u složky ukazuje celkový počet zpráv. Tlačítko **Vyprázdnit** dole pod seznamem je po potvrzení natrvalo smaže ze všech serverů. Smazání zprávy přímo ve Smazaných je definitivní a potvrzuje se. V Nastavení má Smazané vlastní sekci (bez pravidel).
- **Spam** (levý sloupec): spam ze všech účtů na jednom místě, aby šlo najít, co tam spadlo omylem. Číslo u složky ukazuje nepřečtené zprávy. Ve Spamu se z tlačítka Spam stane **Není spam**: zpráva se vrátí do doručené pošty svého účtu a pravidlo spamu pro jejího odesílatele se zruší, aby další zprávy už do spamu nepadaly. Pravidlo se zruší i při přetažení zprávy ze Spamu do doručené pošty nebo do složky. V Nastavení → Spam lze zapnout *Mazat zprávy starší než týden* / *starší než měsíc* (přesun do Smazaných, výchozí: vypnuto).
- **Bayesovský filtr spamu** (Nastavení → Spam): pozná spam podle obsahu i od odesílatelů, kteří ještě nejsou v pravidlech. Učí se z toho, co označíte: tlačítko Spam nebo přetažení do Spamu = spam, **Není spam** = dobrá zpráva. Dobré zprávy se učí i samy: posouzená zpráva, kterou přečtete a necháte 3 dny v doručené poště, se naučí jako dobrá. Napoprvé se filtr naučí z existujících zpráv (spam = zprávy ve spamu od odesílatelů z pravidel, dobré = přečtené zprávy v doručené poště).
  - Posuzuje jen nové nepřečtené zprávy (z posledních 3 dnů). Nikdy nepřesune zprávy od lidí, kterým jste psali, od odesílatelů z pravidel Nedůležitých a Notifikací ani zprávy odpovídající pravidlům složek.
  - Spam začne přesouvat, až zná aspoň 10 spamových a 10 dobrých zpráv. Citlivost: vysoká (hranice 80 %), střední (90 %), nízká (97 %). Filtr jde vypnout; „Zapomenout a naučit znovu“ ho vrátí do výchozího stavu.
  - U zprávy, kterou filtr přesunul do spamu, je v záhlaví štítek „Filtr spamu: 99 %“; po najetí myší ukáže slova, podle kterých rozhodl.
  - Metoda: Robinsonova/Fisherova kombinace pravděpodobností slov (jako SpamBayes), slova z předmětu, odesílatele, vybraných hlaviček, odkazů a prvních 32 kB zprávy. Všechno se počítá a ukládá lokálně v databázi aplikace.
- **Výběr více zpráv:** Ctrl+klik přidá zprávu do výběru nebo ji z něj odebere, Shift+klik vybere rozsah, Ctrl+A vybere všechny načtené zprávy. Vybrané zprávy smažete klávesou **Delete** nebo tlačítkem Smazat, tlačítkem Spam (ve Spamu Není spam) je přesunete hromadně.
- **Drag & drop:** zprávu nebo vybranou skupinu zpráv přetáhněte myší na položku v levém sloupci:
  - na **složku**: přesun do ní (i na jiný účet), pravidlo se nevytváří;
  - na **Smazané**: smazání;
  - na **Spam**: přesun do spamu, odesílatelé se přidají do pravidel spamu;
  - na **účet**: přesun do doručené pošty tohoto účtu;
  - na **Nedůležité** nebo **Notifikace**: pro odesílatele vznikne pravidlo této složky;
  - na **Důležité** nebo **Vzít na vědomí**: zprávy přestanou být nedůležité / notifikacemi (zruší se i pravidla, která jim odpovídají);
  - ze Smazaných nebo ze složky na kterýkoli pohled doručené pošty: obnovení do doručené pošty.

  Pokud by zprávy po přesunu hned zmizely, aplikace se předem zeptá. Jde o tyto případy: staré nedůležité zprávy nebo notifikace by se podle nastavení smazaly, odesílatel je označený jako spam, nebo zprávu hned přesune pravidlo složky.

- **Kontextové menu** (pravé tlačítko myši na položce levého panelu):
  - **Označit vše jako přečtené**: platí pro pohledy doručené pošty, složky i účty. V aplikaci se změna projeví hned, server ji dostane na pozadí.
  - **Nastavení**: u složky otevře její obrazovku v Nastavení, u účtu nastavení účtu, u Nedůležitých, Notifikací, Spamu a Smazaných jejich sekci Nastavení.
  - Položky, které u dané složky nedávají smysl, jsou zašedlé (např. u Smazaných).

Databázi lze kdykoli smazat (při zavřené aplikaci). Při dalším spuštění se znovu stáhne ze serverů. Přijdete jen o odložení, pravidla pro spam, nedůležité, notifikace a složky a o konfiguraci složek (samotné složky a zprávy na serveru zůstanou).

### Kde jsou data

Nastavení účtů je uložené v `%APPDATA%\Mailer\` (název složky zůstal z doby, kdy se aplikace jmenovala Mailer), hesla a tokeny ve Správci pověření Windows. Nastavení ze starší verze (složka `data\` vedle zdrojových kódů) se při prvním spuštění zkopíruje automaticky.

## Co umí

- Libovolný počet účtů, jejichž doručená pošta se slučuje do pohledů **Důležité**, **Vzít na vědomí**, **Nedůležité** a **Notifikace** (účty se načítají paralelně a jsou barevně odlišené). Kliknutí na účet v levém sloupci otevře jeho nastavení.
- Nastavitelnou šířku sloupců: levý panel, seznam zpráv a kontext se dají rozšířit nebo zúžit tažením za okraj. Dvojklik na okraj vrátí výchozí šířku. Sloupec s tlačítky má pevnou šířku, čtení zprávy vyplní zbytek okna. Šířky se pamatují i po restartu.
- Přepínač **Jen nepřečtené** v záhlaví seznamu skryje přečtené zprávy. Zpráva, kterou právě čtete, v seznamu zůstane. Přepínač platí jen do opuštění složky, po návratu jsou zase vidět všechny zprávy.
- **Hledání ve veškeré poště** (pole nahoře přes všechny sloupce kromě levého): hledá ve všech účtech a složkách. Hned ukáže shody v uložených zprávách (předmět, odesílatel, příjemci) i se složkou, kde zprávu najdete. Potom na serverech dohledá shody v textu zpráv a zprávy, které ještě nejsou stažené, a přidá je pod oddělovač. Smazáním textu (nebo Esc) se vrátíte, kde jste byli.
- **Filtr** (pole v záhlaví seznamu): při psaní hned filtruje zprávy v aktuální složce z uložených zpráv, potom stejně dohledá další na serveru.
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
- Aktualizace jsou podepsané (Ed25519, `ed25519.py` podle RFC 8032). Veřejný klíč je zabudovaný v aplikaci, soukromý má jen vývojář, takže ani převzatý účet na GitHubu nemůže uživatelům podstrčit cizí program. Stažený exe musí sedět na velikost a SHA-256 z podepsaného `latest.json`, a aplikace nikdy nepřejde na starší verzi.

## Gmail – přihlášení přes Google (OAuth)

Gmail se dá připojit bez hesla tlačítkem **Přihlásit přes Google**. Aplikace k tomu potřebuje vlastního *OAuth klienta*. Jednorázově ho vytvoříte takto (asi 10 minut):

1. Otevřete <https://console.cloud.google.com/> a vytvořte nový projekt (např. „Norman Mail“).
2. **APIs & Services → Library**: vyhledejte **Gmail API** a klikněte na **Enable**.
3. **APIs & Services → OAuth consent screen** (v novějším rozhraní *Google Auth Platform*):
   - User type: **External**, vyplňte název aplikace a svůj email.
   - V části **Audience / Test users** přidejte svou Gmail adresu (i všechny další adresy, které chcete připojit).
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type: **Desktop app**. Redirect URI se nezadává, protože aplikace používá `http://127.0.0.1:<port>/oauth/callback`, který Google u desktopových klientů povoluje automaticky.
   - Zkopírujte **Client ID** a **Client Secret**.
5. V aplikaci Norman Mail klikněte na **+ Přidat účet**, zvolte Gmail a **Přihlásit přes Google**. Při prvním použití vložte Client ID a Client Secret. Uloží se do `%APPDATA%\Mailer\google_client.json`.
6. V okně Googlu se přihlaste a povolte přístup. Google zobrazí varování „Google tuto aplikaci neověřil“. Je to v pořádku, protože jde o vaši vlastní aplikaci: klikněte na **Pokračovat**.

Obnovovací token se uloží do Správce pověření Windows. Krátkodobé přístupové tokeny si aplikace obnovuje sama.

> **Důležité:** dokud je aplikace v Google Cloud ve stavu **Testing**, platnost přihlášení vyprší po 7 dnech a bude potřeba se přihlásit znovu (Nastavení účtu → Přihlásit znovu). Tomu se vyhnete, když na stránce OAuth consent screen / Audience kliknete na **Publish app** (stav „In production“). Pro osobní použití není nutné ověření aplikace u Googlu, jen zůstane zobrazené varování o neověřené aplikaci.

Přihlášení můžete kdykoli zrušit na <https://myaccount.google.com/permissions>.

## Poznámky k poskytovatelům

- **Gmail (alternativa bez OAuth), iCloud a Yahoo:** místo běžného hesla je potřeba vytvořit *heslo aplikace*.
- **Seznam:** v nastavení schránky musí být povolený přístup přes IMAP.
- **Outlook.com / Microsoft 365:** Microsoft u většiny účtů zakázal přihlašování heslem a vyžaduje OAuth, které tento jednoduchý klient nepodporuje.

## Jazyky a překlady

Rozhraní je v angličtině (základní jazyk) a češtině; obojí je zabudované v `NormanMail.exe` a aktualizuje se s aplikací. Jazyk se vybírá v **Nastavení → O aplikaci**. Výchozí volba *Automaticky* použije jazyk Windows, pokud pro něj aplikace má překlad, jinak angličtinu.

**Vlastní jazyk bez nového sestavení:** v **Nastavení → O aplikaci** klikněte na *Uložit šablonu překladu*. Do složky s jazyky se uloží `_template.yaml` s celou angličtinou. Přejmenujte ho podle kódu jazyka (např. `de.yaml`), v oddílu `meta` změňte `language` a `name` a přeložte texty. Aplikace soubor najde při příštím otevření Nastavení a nabídne ho ve výběru jazyků. Nepřeložené texty zůstanou anglicky (Nastavení ukáže, kolik jich chybí). Soubor se stejným kódem jako vestavěný jazyk (třeba `cs.yaml`) přepíše jen texty, které obsahuje.

Soubory s jazyky se hledají ve složce `languages` vedle `NormanMail.exe` a ve složce `languages` ve složce s daty (`%APPDATA%\Mailer\languages`, v portable režimu `data\languages`). Soubory začínající podtržítkem se přeskakují.

Formát je zjednodušený YAML, který zná jen texty, takže `no`, `yes` ani čísla se nepřevádějí:

```yaml
meta:
  language: de           # kód jazyka
  name: Deutsch          # název ve výběru jazyků
  locale: de-DE          # formát data a času
nav.folders: Ordner
messages.count:          # množné číslo: zero, one, two, few, many, other (podle jazyka)
  one: "{count} Nachricht"
  other: "{count} Nachrichten"
about.text: |
  Víceřádkový text.
```

- `{name}` jsou proměnné, které doplní aplikace; musí v překladu zůstat.
- Text s dvojtečkou a mezerou nebo začínající znakem jako `[`, `{`, `*` dejte do uvozovek.
- Jediné formátování je `**tučně**`. Texty se vždy vkládají jako prostý text: HTML, odkazy ani skripty z překladu se neprovedou, takže ani soubor stažený z internetu nemůže podvrhnout ovládací prvky.
- Soubor s chybou se nepoužije vůbec. Nastavení ukáže soubor a číslo řádku s chybou.

**Pro vývojáře:** texty jsou v `static/i18n/en.yaml` a `cs.yaml`. Ve frontendu `t('klíč', {proměnné})` a `tn('klíč', počet)`, ve statickém HTML atributy `data-i18n`, `data-i18n-title`, `data-i18n-placeholder` a `data-i18n-aria-label`, v Pythonu `tr("klíč", proměnná=…)` (`from i18n import t as tr`). Test `tests/test_i18n.py` hlídá, že každý klíč použitý v kódu je v obou katalozích.

## Struktura

| Soubor | Účel |
|---|---|
| `main.py` | Desktopová aplikace (okno, ukládání příloh) |
| `instance.py` | Jediná instance aplikace, úklid dočasných složek sestavené verze |
| `taskbar.py` | Identita aplikace na hlavním panelu, zástupci |
| `updater.py`, `ed25519.py` | Automatické aktualizace z GitHub Releases, ověření podpisu |
| `app.py` | Jádro aplikace (Flask endpointy `/api/*`), volané přes most pywebview |
| `tools/dev_server.py` | Vývojový server pro ladění v prohlížeči (není v `NormanMail.exe`) |
| `google_oauth.py` | Přihlášení přes Google (OAuth) |
| `i18n.py`, `static/i18n/` | Lokalizace: načítání katalogů textů (vestavěných i uživatelských), anglický a český katalog |
| `tests/` | Testy (`.venv\Scripts\python -m unittest discover tests`) |
| `tools/release.py` | Vydání nové verze (sestavení, podepsaný `latest.json`, GitHub release) |
| `tools/i18n_add.py` | Pomocník pro přidání textů do obou vestavěných katalogů |
| `db.py` | Lokální databáze metadat (SQLite) |
| `sync.py` | Synchronizace se servery na pozadí, probouzení odložených zpráv |
| `mail.py` | Práce s IMAP a SMTP, parsování zpráv, sdílení spojení |
| `store.py` | Ukládání účtů a hesel |
| `static/` | Uživatelské rozhraní (HTML, CSS, JS) |
| `build.bat`, `NormanMail.spec`, `version.py` | Sestavení `NormanMail.exe` |
| `assets/portable.txt` | Značka portable režimu (položte vedle `NormanMail.exe`) |

Port lze změnit proměnnou prostředí `MAILER_PORT`, složku s daty proměnnou `MAILER_DATA`.
