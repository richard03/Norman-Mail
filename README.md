# Norman Mail

Jednoduchý desktopový emailový klient pro Windows, který obslouží více účtů najednou (IMAP + SMTP).

## Spuštění

**Pro Windows bez Pythonu** (Windows 10/11, 64bit): celá aplikace je jediný soubor `NormanMail.exe` (asi 12 MB, obsahuje vše potřebné včetně Pythonu). Nic se neinstaluje: uložte ho kamkoli a spusťte. Zástupce v nabídce Start nebo na ploše vytvoříte v **Nastavení → O aplikaci**. Data jsou v `%APPDATA%\Mailer`, hesla ve Správci pověření Windows. Aplikaci odstraníte smazáním souboru (a zástupců).
- **Portable režim:** když vedle `NormanMail.exe` položíte soubor `portable.txt` (je v `assets\`), ukládá aplikace všechna data přímo do složky, ve které leží (verze do 1.0.2 je měly v podsložce `data`, při prvním spuštění se přesunou), např. na flash disku. Hesla jsou z bezpečnostních důvodů ve Správci pověření Windows daného počítače, takže na jiném počítači je zadáte znovu.
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
  Filtr v záhlaví sloupce vybere **Všechno**, **Diskusní vlákno** (podle hlaviček In-Reply-To a References, takže najde i odpovědi se změněným předmětem), **Stejný odesílatel** (zprávy od protistrany, moje zprávy jí a zprávy s ní v kopii) nebo **Stejný předmět**. Každý druh má vlastní limit 100 zpráv. Hlavičky vlákna se u dříve stažené pošty doplňují na pozadí; do té doby se vlákno pozná podle stejného předmětu a protistrany.
  Kliknutím na zprávu v kontextu se zpráva otevře a zároveň se nalistuje v seznamu vlevo. Aplikace ji hledá v tomto pořadí: složka aplikace → Důležité / Vzít na vědomí / Nedůležité / Notifikace → Spam / Smazané. Zprávu z ostatních složek (Odeslané, Archiv…) ukáže v dané složce účtu.
- **Odložené zprávy:** akce *Odložit o den* / *Odložit o týden* označí zprávu jako přečtenou a zítra, resp. za týden v 8:00, ji zase označí jako nepřečtenou. Pokud aplikace v tu chvíli neběží, stane se to hned po dalším spuštění.

- **Podmínky pravidel** jsou u Nedůležitých, Notifikací, Spamu i vlastních složek stejné: odesílatel, příjemce a předmět; vyplněné podmínky musí platit všechny. Místo adresy odesílatele stačí doména (např. `github.com` – platí i pro subdomény). U předmětu se volí, zda stačí jeho část, nebo musí odpovídat přesně (bez předpon Re:/Fw:, bez ohledu na velikost písmen). Pravidla se přidávají tlačítkem *Přidat pravidlo* nad seznamem pravidel v Nastavení a upravují ve stejném okně.
- **Ruční přesun má vždy přednost.** Kdykoli zprávu ručně přesunete (tlačítkem *Nedůležité*, *Notifikace*, *Důležité*, *Spam* nebo přetažením), aplikace se zeptá, jestli přesunout **jen tuto zprávu**, nebo nastavit pravidlo (*Vše od odesílatele*, *Vše s předmětem*, u Nedůležitých / Notifikací i *Vlastní pravidlo…*). Ručně přesunutou zprávu v doručené poště už automatické řazení nepřesune: pravidla kategorií, složek a spamu ani bayesovský filtr (platí i pro zprávu vrácenou ze složky nebo ze spamu). Nové pravidlo „vše od odesílatele / s předmětem“ nahradí jednoduchá pravidla se stejnou podmínkou, která zprávy posílala jinam (dialog ukáže, která). Při přesunu do **Důležité**, **Vzít na vědomí** nebo do doručené pošty se pravidla nemažou; když zprávu nějaké pravidlo řadí jinam, dialog nabídne i *Zrušit i pravidla, která ji řadí jinam*. Když zprávě odpovídá víc pravidel, vyhrává důležitější cíl: vlastní složka, Nedůležité, Notifikace, Spam (pravidlo spamu ani filtr nepřesune zprávu, kterou řadí jiné pravidlo). Vpravo nahoře v záhlaví otevřené zprávy je ikona ⓘ: po najetí myší a po kliknutí (v okně) vysvětlí, proč je zpráva tady – ručně přesunutá, pravidlo, které ji sem zařadilo (i s upozorněním, že jste ho mezitím změnili nebo smazali), bayesovský filtr (s jistotou a slovy, podle kterých rozhodl), nebo výchozí řazení podle pole Komu / kopie. Vysvětlení se týká jen místa, kde zpráva je.
- **Pravidla pro spam:** označením zprávy jako *Spam* s volbou *Vše od odesílatele* vznikne pravidlo „odesílatel je spam“. Nové zprávy od tohoto odesílatele synchronizace ve všech účtech přesune do spamu dřív, než se objeví v seznamu. Pravidla jsou přehledně v **Nastavení → Složky → Spam**, kde je lze přidávat (*Přidat pravidlo*), upravovat a mazat. Pro vlastní adresy pravidla nevznikají.
- **Vzít na vědomí:** zprávy z doručené pošty, ve kterých nejste v poli Komu, tedy jste v kopii nebo ve skryté kopii. Tyto zprávy se nezobrazují v **Důležité**. Nedůležité a Notifikace mají přednost.
- **Nedůležité / Notifikace:** tlačítka *Nedůležité* a *Notifikace* (sloupec s tlačítky, sekce *Třídění*) nabídnou, co zařadit:
  - *Jen tato zpráva*: zařadí jen tuto zprávu, pravidlo nevzniká;
  - *Vše od odesílatele*: pravidlo pro všechny dosavadní i budoucí zprávy od odesílatele;
  - *Vše s předmětem*: pravidlo pro všechny zprávy, jejichž předmět obsahuje předmět této zprávy (bez předpon Re:/Fw:, bez ohledu na velikost písmen).

  Zprávy se pak zobrazují ve složce **Nedůležité**, resp. **Notifikace**, a ne v **Důležité** (výchozí pohled po startu). Každá zpráva je v levém sloupci vidět právě v jednom seznamu, souhrnné pohledy typu „Všechny doručené“ záměrně nejsou. Zprávy zůstávají na serveru v doručené poště, jde jen o lokální třídění. Ruční zařazení zprávy má přednost před pravidly; když nové zprávě odpovídají pravidla obou složek, vyhrají Nedůležité. Zařazení pravidlem se zapamatuje ve chvíli, kdy proběhne (nová zpráva, založení pravidla): smazání nebo změna pravidla už zařazené zprávy nevrátí, platí jen pro zprávy, které přijdou potom. Ve **Vzít na vědomí**, **Nedůležitých** a **Notifikacích** je tlačítko *Důležité*: zpráva se přesune do Důležitých (i když je jen v kopii); pravidla, podle kterých se řadila jinam, se zruší jen na přání. Tlačítka se řídí tím, kde zpráva je: v **ToDo** chybí Třídění a odložení, v **Odeslaných** Třídění, ToDo, odložení a archivace, ve **Spamu** zůstane jen *Není spam* a *Smazat*, ve **Smazaných** jen *Obnovit* (vrátí zprávu do doručené pošty jejího účtu). Zpráva, která po zařazení ze seznamu zmizí, zůstane otevřená. Pravidla lze mazat v **Nastavení → Složky → Nedůležité**, resp. **Notifikace**.
- **Nastavení Nedůležitých a Notifikací** (každá složka zvlášť):
  - *Když opustím sekci …, označit všechny zprávy jako přečtené* (výchozí: vypnuto). Uplatní se při přepnutí na jiný pohled.
  - *Mazat přečtené zprávy starší než týden* / *starší než měsíc*.
  - *Mazat nepřečtené zprávy starší než týden* / *starší než měsíc*.

  Všechny mazací volby jsou ve výchozím stavu vypnuté. Když je zaškrtnutý týden i měsíc, platí kratší lhůta.

  Mazání probíhá v rámci synchronizace (přesun do koše). Odložené zprávy se nemažou.
- **Složky** (levý sloupec, *+ Přidat složku*): každá složka v aplikaci míří na **jednu konkrétní složku na jednom účtu**, buď existující na serveru, nebo novou. Konfigurace složek je v **Nastavení → Složky** pod vestavěnými složkami (ToDo … Smazané). Každá složka tam má svou obrazovku: uprostřed je konfigurace, vpravo pravidla. Tlačítko ⚙ u složky v levém sloupci otevře Nastavení rovnou na obrazovce této složky. V konfiguraci nastavíte cílový účet a složku a zda zprávy přesunuté ihned po doručení označit jako přečtené. Vpravo jsou pravidla (tlačítko *Přidat pravidlo*, úprava a mazání). Každé pravidlo má vlastní nastavení, kdy přesouvat: *ihned po doručení* nebo *až po přečtení*.
  - **Nastavit pravidlo…** (sloupec s tlačítky, sekce *Třídění*): otevře okno, kde u zprávy zvolíte složku, kdy přesouvat a podle čeho poznat další zprávy (odesílatel, příjemce, část předmětu). Zpráva se do složky přesune hned a pravidlo platí pro další zprávy.
  - Typy pravidel: **odesílatel**, **příjemce** (adresa v Komu nebo Kopii, např. když jste v kopii a příjemcem je kolega) a **slovo či fráze v předmětu** (bez ohledu na velikost písmen a předpony Re:/Fw:).
  - Pravidla platí pro doručenou poštu všech účtů. Zprávy z jiného účtu se přenesou na cílový účet (s původním datem a příznaky) a na původním účtu skončí v **koši**. Když uložení na cílový server selže, ze zdroje se nic nemaže.
  - Odložené zprávy se nepřesouvají. Odesílatel může patřit jen do jedné složky. *Smazat složku* nabídne volbu: **jen v aplikaci** (složka i zprávy na serveru zůstanou), nebo **i na serveru včetně všech zpráv** (zprávy se přesunou do koše účtu, pak se složka smaže, potvrzuje se druhým kliknutím).
  - Složky z dřívější verze bez cílového účtu se nepoužívají, dokud jim účet nenastavíte (v levém sloupci mají červený vykřičník).

- **Nastavení** má tři části: **O aplikaci**, **Složky** a **Účty**. U Složek a Účtů je druhý sloupec se seznamem (všechny složky včetně vlastních, resp. přidané účty). Po kliknutí na položku se zobrazí její nastavení a případně pravidla; složky bez nastavení (ToDo, Důležité, Vzít na vědomí, Odeslané) ukážou krátké vysvětlení, k čemu slouží a které zprávy do nich patří. Nastavení účtu (kliknutí na účet v levém sloupci) se otevírá v Nastavení → Účty, nový účet se přidává v samostatném okně.
- **Úprava pravidel:** v Nastavení má každé pravidlo tlačítko *Upravit*.
  - U Nedůležitých a Notifikací se otevře okno se třemi poli: *Odesílatel*, *Příjemce* (adresa v Komu nebo Kopii) a *Část předmětu*. Zpráva musí splnit všechna vyplněná pole, prázdné pole se nekontroluje. Zprávy zařazené podle původního znění zůstanou, kde jsou. Když novému znění odpovídají starší zprávy v doručené poště, aplikace se zeptá, jestli je zařadit také (*Přesunout i starší* / *Jen nové zprávy*).
  - U Spamu a složek se pravidlo upravuje přímo v řádku (typ a hodnota, u složek i kdy přesouvat). *Uložit* (Enter) změnu uloží, *Zrušit* (Esc) ji zahodí.
  - Stejné pravidlo nejde v jednom seznamu mít dvakrát.
- **Smazané** (levý sloupec): smazané zprávy ze všech účtů na jednom místě (koše jednotlivých účtů). Zprávy v této složce se automaticky označí jako přečtené, i když je tam přesunul jiný klient. Číslo u složky ukazuje celkový počet zpráv. Tlačítko **Vyprázdnit** dole pod seznamem je po potvrzení natrvalo smaže ze všech serverů. Smazání zprávy přímo ve Smazaných je definitivní a potvrzuje se. V Nastavení → Složky mají Smazané vlastní obrazovku (bez pravidel) s volbami *Natrvalo mazat zprávy starší než týden* / *měsíc* / *rok* (výchozí: vypnuto). Mazání probíhá v rámci synchronizace, je definitivní a stáří se počítá od data zprávy, ne od smazání.
- **Spam** (levý sloupec): spam ze všech účtů na jednom místě, aby šlo najít, co tam spadlo omylem. Číslo u složky ukazuje nepřečtené zprávy. Ve Spamu se z tlačítka Spam stane **Není spam**: zpráva se vrátí do doručené pošty svého účtu. Pravidlo spamu pro jejího odesílatele zůstane, pokud v dialogu nezvolíte *Zrušit i pravidla, která ji řadí jinam*; vrácenou zprávu ale už do spamu nepřesune. V Nastavení → Složky → Spam lze zapnout *Mazat zprávy starší než týden* / *starší než měsíc* / *starší než rok* (přesun do Smazaných, výchozí: vypnuto; platí nejkratší zaškrtnutá lhůta).
- **Bayesovský filtr** (Nastavení → O aplikaci, poslední sekce *Bayesovský filtr*; `bayes.py`): jeden filtr pro spam, doručenou poštu, Nedůležité, Notifikace a každou složku aplikace. Čte hlavičky i text (prvních 32 kB zprávy). U nových zpráv v doručené poště (z posledních 3 dnů) se obsah stáhne jednou a slouží učení i posuzování.
  - **Učí se jen z vašich rozhodnutí:** tlačítko Spam nebo přetažení do Spamu, **Není spam**, ruční přesuny (do kategorie, Důležitých, Vzít na vědomí, složky; jen zpráva i s pravidlem) a potvrzené zprávy. Potvrzená je zpráva, která po 3 dnech leží tam, kam ji dal uživatel nebo jeho pravidlo: přečtená v doručené poště, v kategorii podle ručního umístění / pravidla, ve složce aplikace. Zprávy, které zařadil sám filtr, se do učení nepočítají, dokud je ručně nepotvrdíte. Napoprvé (a u nové složky) se naučí z existujících zpráv: spam od odesílatelů z pravidel, přečtená doručená pošta, zprávy v Nedůležitých a Notifikacích, obsah složek (nejvýš 300 na třídu).
  - **Pořadí rozhodování:** ruční přesun > explicitní pravidlo > bayesovský filtr > výchozí řazení podle pole Komu. Filtr posuzuje jen nové nepřečtené zprávy, o kterých nerozhodlo ruční umístění ani pravidlo. Když je jistý víc cílů, vyhrává důležitější: doručená pošta, složka, Nedůležité, Notifikace, Spam. Jistá doručená pošta = nic se nepřesouvá.
  - **Kam smí řadit:** do spamu, když je filtr zapnutý (citlivost: vysoká 80 %, střední 90 %, nízká 97 %; ověřené zprávy od lidí, kterým jste psali, nikdy). Do Nedůležitých a Notifikací s jistotou aspoň 95 %, když je to v jejich nastavení zapnuté. Vypnutí filtru nebo řazení do kategorie platí jen pro nově příchozí zprávy; už zařazené zprávy zůstanou, kde jsou (i natrvalo, zařazení se nezapomíná). Do složky jen se zapnutým *Bayesovský filtr smí do této složky řadit nové zprávy sám*; jinak se v záhlaví podobné zprávy v doručené poště ukáže návrh „Podobné zprávy dáváte do: X (93 %) – Přesunout“. Když vítězný cíl řadit nesmí, zpráva zůstane, kde je.
  - Třída se posuzuje, až má aspoň 10 naučených zpráv (a doručená pošta taky). Stav učení po třídách je v Nastavení; *Zapomenout a naučit znovu* ho vrátí do výchozího stavu.
  - U zprávy zařazené filtrem ukáže ikona ⓘ v záhlaví jistotu a slova, podle kterých rozhodl. Ruční přesun rozhodnutí filtru přebije a filtr se z něj učí.
  - Metoda: každá třída se posoudí Robinsonovou/Fisherovou metodou „tato třída proti ostatním“ (jako SpamBayes): vyhlazené pravděpodobnosti nejvýraznějších slov se zkombinují chí-kvadrát testem, nejisté zprávy vycházejí kolem 50 %. Slova z předmětu, odesílatele, vybraných hlaviček, odkazů a textu. Všechno se počítá a ukládá lokálně v databázi aplikace. Dřívější naučená data filtru spamu se převedla (spam / dobré → třídy spam / doručená pošta).
- **Výběr více zpráv:** Ctrl+klik přidá zprávu do výběru nebo ji z něj odebere, Shift+klik vybere rozsah, Ctrl+A vybere všechny načtené zprávy. Vybrané zprávy smažete klávesou **Delete** nebo tlačítkem Smazat, tlačítkem Spam (ve Spamu Není spam) je přesunete hromadně.
- **Drag & drop:** zprávu nebo vybranou skupinu zpráv přetáhněte myší na položku v levém sloupci:
  - na **složku**: přesun do ní (i na jiný účet), případně s pravidlem složky pro odesílatele / předmět;
  - na **Smazané**: smazání (bez dotazu na pravidlo);
  - na **Spam**: přesun do spamu, případně s pravidlem spamu pro odesílatele;
  - na **účet**: přesun do doručené pošty tohoto účtu;
  - na **Nedůležité** nebo **Notifikace**: zařazení, případně s pravidlem této složky;
  - na **Důležité** nebo **Vzít na vědomí**: zprávy se přesunou sem, pravidla se zruší jen na přání;
  - ze Smazaných nebo ze složky na kterýkoli pohled doručené pošty: obnovení do doručené pošty.

  Dialog přesunu upozorní i na to, co by se se zprávami hned stalo, např. že by se staré nedůležité zprávy nebo notifikace podle nastavení smazaly.

- **Kontextové menu** (pravé tlačítko myši na položce levého panelu):
  - **Označit vše jako přečtené**: platí pro pohledy doručené pošty, složky i účty. V aplikaci se změna projeví hned, server ji dostane na pozadí.
  - **Nastavení**: otevře obrazovku složky nebo účtu v Nastavení (u ToDo, Důležitých, Vzít na vědomí a Odeslaných jen vysvětlení, k čemu složka slouží).
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

Gmail se připojí bez hesla tlačítkem **Přihlásit přes Google**: v prohlížeči se přihlásíte ke svému účtu Google a povolíte aplikaci Norman Mail přístup k poště. Adresa i servery se vyplní samy. Norman Mail zatím není u Googlu ověřený, takže Google při přihlášení upozorní na neověřenou aplikaci. Stačí rozbalit pokročilé možnosti a pokračovat do aplikace Norman Mail. Obnovovací token se uloží do Správce pověření Windows, krátkodobé přístupové tokeny si aplikace obnovuje sama.

**Pro vývojáře:** OAuth klient aplikace (Client ID a Client Secret) není v repozitáři. Leží v `%USERPROFILE%\.normanmail\google_client.json` (jiné umístění: proměnná `NORMANMAIL_GOOGLE_CLIENT`) a `NormanMail.spec` ho při sestavení přibalí do exe. Při spuštění ze zdrojáků se použije přímo tento soubor. `tools/release.py` bez něj vydání odmítne. Stačí do souboru uložit JSON stažený z Google Cloud Console (Credentials → OAuth client typu **Desktop app** → Download JSON). U desktopových aplikací Google Client Secret za tajný nepovažuje, bezpečnost přihlášení zajišťuje PKCE. Nastavení projektu v Google Cloud (stav In production bez ověření, limit 100 uživatelů) a postup pozdějšího ověření jsou v [docs/google-verification.md](docs/google-verification.md).

Sestavení bez vestavěného klienta se při prvním přihlášení zeptá na Client ID a Client Secret vlastního klienta a uloží je do `%APPDATA%\Mailer\google_client.json`. Ten má vždy přednost před vestavěným klientem (tokeny vydané pro jednoho klienta s jiným obnovit nejdou). Vlastního klienta si vytvoříte v <https://console.cloud.google.com/>: nový projekt, zapnout **Gmail API**, OAuth consent screen typu **External** (svou adresu přidejte mezi testery) a OAuth client ID typu **Desktop app**. Dokud je takový projekt ve stavu **Testing**, přihlášení vyprší po 7 dnech.

Přihlášení můžete kdykoli zrušit na <https://myaccount.google.com/permissions>.

## Poznámky k poskytovatelům

- **Gmail (alternativa bez OAuth), iCloud a Yahoo:** místo běžného hesla je potřeba vytvořit *heslo aplikace*.
- **Seznam:** v nastavení schránky musí být povolený přístup přes IMAP.
- **Outlook.com / Microsoft 365:** Microsoft u většiny účtů zakázal přihlašování heslem a vyžaduje OAuth, které tento jednoduchý klient nepodporuje.

## Jazyky a překlady

V `NormanMail.exe` je zabudovaná jen angličtina (základní jazyk), která se aktualizuje s aplikací. Ostatní jazyky jsou samostatné soubory. Čeština je v repozitáři jako `languages/cs.yaml` a je přiložená ke každému vydání na GitHubu (`cs.yaml`, kontrolní součet v podepsaném `latest.json`). Oficiální překlady jde stáhnout přímo v **Nastavení → O aplikaci → Jazyk** (položky *(stáhnout)*), nebo soubor ručně dát do podsložky `languages` datové složky. **Aktualizace překladů:** po aktualizaci aplikace si ta (se zapnutými aktualizacemi) stáhne překlad k nové verzi a sloučí ho se souborem uživatele: texty, které uživatel neupravil, dostanou nové znění, jeho úpravy zůstanou, nové texty přibudou. Úpravy pozná porovnáním s oficiálním zněním z vydání, ke kterému soubor patří (`app_version` v oddílu `meta`); soubor bez `app_version` se porovná se všemi vydáními na GitHubu. Před přepsáním se soubor zazálohuje jako `_cs.yaml.bak`. Stažený překlad se použije, jen když sedí na kontrolní součet z podepsaného `latest.json`. Bez internetu zůstane dosavadní soubor a zkusí se to znovu za hodinu. Vydání na GitHubu proto nemažte – slouží jako podklad pro porovnání (`langupdate.py`). Jazyk se vybírá v **Nastavení → O aplikaci**. Nová instalace začíná anglicky. Pokud ale v podsložce `languages` datové složky už leží překlad, aplikace hned při prvním spuštění nabídne okno s výběrem jazyka. Kdo aplikaci používal před verzí 1.0.3 (kdy se jazyk řídil Windows), dostane natrvalo jazyk, který viděl dosud.

**Vlastní jazyk bez nového sestavení:** z [vydání na GitHubu](https://github.com/richard03/Norman-Mail/releases/latest) si stáhněte `translation-template.yaml` (celá angličtina) a uložte ho do složky s jazyky (**Nastavení → O aplikaci → Otevřít složku s jazyky**, tedy podsložka `languages` datové složky) pod kódem jazyka (např. `de.yaml`), v oddílu `meta` změňte `language` a `name` a přeložte texty. Aplikace soubor najde při příštím otevření Nastavení a nabídne ho ve výběru jazyků. Nepřeložené texty zůstanou anglicky (Nastavení ukáže, kolik jich chybí). Soubor `en.yaml` přepíše jen ty anglické texty, které obsahuje.

Soubory s jazyky se hledají jen v podsložce `languages` datové složky aplikace (`%APPDATA%\Mailer\languages`, v portable režimu `languages` vedle programu), takže lokální data i překlady jsou vždy pohromadě. Cestu k datové složce ukazuje **Nastavení → O aplikaci**. Soubory začínající podtržítkem se přeskakují.

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

**Pro vývojáře:** texty jsou v `static/i18n/en.yaml` (zabudovaná angličtina) a `languages/cs.yaml` (čeština, `tools/release.py` ji přikládá k vydání). Ve frontendu `t('klíč', {proměnné})` a `tn('klíč', počet)`, ve statickém HTML atributy `data-i18n`, `data-i18n-title`, `data-i18n-placeholder` a `data-i18n-aria-label`, v Pythonu `tr("klíč", proměnná=…)` (`from i18n import t as tr`). Test `tests/test_i18n.py` hlídá, že každý klíč použitý v kódu je v obou katalozích.

## Struktura

| Soubor | Účel |
|---|---|
| `main.py` | Desktopová aplikace (okno, ukládání příloh) |
| `instance.py` | Jediná instance aplikace, úklid dočasných složek sestavené verze |
| `taskbar.py` | Identita aplikace na hlavním panelu, zástupci |
| `updater.py`, `ed25519.py` | Automatické aktualizace z GitHub Releases, ověření podpisu |
| `app.py` | Jádro aplikace (Flask endpointy `/api/*`), volané přes most pywebview |
| `tools/dev_server.py` | Vývojový server pro ladění v prohlížeči (není v `NormanMail.exe`) |
| `google_oauth.py`, `google_client.py` | Přihlášení přes Google (OAuth), načtení OAuth klienta aplikace |
| `site/` | Web aplikace se zásadami ochrany soukromí a podmínkami použití (GitHub Pages, `.github/workflows/pages.yml`) |
| `i18n.py`, `static/i18n/`, `languages/` | Lokalizace: načítání katalogů textů, zabudovaná angličtina, čeština jako samostatný soubor |
| `langupdate.py` | Stažení oficiálních překladů z GitHub Releases a jejich aktualizace se zachováním úprav uživatele |
| `tests/` | Testy (`.venv\Scripts\python -m unittest discover tests`) |
| `tools/release.py` | Vydání nové verze (sestavení, podepsaný `latest.json`, GitHub release) |
| `tools/make_icon.py` | Z předlohy `assets/mailer-256.png` vygeneruje `assets/mailer.ico`, logo 120×120 a ikony webu `site/` (potřebuje Pillow) |
| `tools/i18n_add.py` | Pomocník pro přidání textů do obou vestavěných katalogů |
| `db.py` | Lokální databáze metadat (SQLite) |
| `sync.py` | Synchronizace se servery na pozadí, probouzení odložených zpráv |
| `bayes.py` | Bayesovský filtr: spam, doručená pošta, Nedůležité, Notifikace a složky v jednom modelu |
| `mail.py` | Práce s IMAP a SMTP, parsování zpráv, sdílení spojení |
| `store.py` | Ukládání účtů a hesel |
| `static/` | Uživatelské rozhraní (HTML, CSS, JS) |
| `build.bat`, `NormanMail.spec`, `version.py` | Sestavení `NormanMail.exe` |
| `assets/portable.txt` | Značka portable režimu (položte vedle `NormanMail.exe`) |

Port lze změnit proměnnou prostředí `MAILER_PORT`, složku s daty proměnnou `MAILER_DATA`.
