# Brief commun — préparation des jeux de génotypage (lot 2, catalogue complet)

Tu prépares et valides des jeux de génotypage publics pour data-explorer et metadata-api de Genovalia, selon la méthode déjà verrouillée. **Préparer et valider seulement : aucune injection** (ni BD, ni API, ni Pydio).

## À lire d'abord
- `/home/steve/git/genovalia/data-explorer/.claude/skills/genotyping-curation/SKILL.md` et `references/lessons.md` (méthode, règles tirées des bugs passés, taxonomie des problèmes).
- `~/genovalia-injection/build.py` : helpers (`P`, `CC0`, `CCBY`, `link`, `write_vcf`, `letters_to_rows`, `_dna`, `_vcf_samples`, `ATTR`, `UCUM`, `vcf_stats`) et exemples complets (`prepare_malvil1`, `prepare_anogla1`, `prepare_salfon1`, `prepare_picgla2`, `prepare_triaes1` et leurs entrées `DATASETS`). Copie leur style.
- Métadonnées de dépôt déjà récupérées : `~/genovalia-injection/triage/meta/<doi avec _>.json` (fichiers et tailles, auteurs + ORCID, licence, travaux liés, résumé). Ne rappelle pas l'API Dryad pour ça.

## Ce que tu produis, pour chaque jeu assigné
1. `~/genovalia-injection/prep/<id>.py`, **un module par jeu** (ne modifie jamais `build.py`, `fetch.py`, `papers.py`, `SUMMARY.md` ni le dossier d'un autre jeu) :
   ```python
   FILES = [(url, nom_local), ...]          # lu par fetch.py
   MANUAL = [(id, document, url, dest, raison), ...]   # ce qui n'a pas pu être téléchargé (optionnel)
   def CFG(h): return dict(species=..., taxid=..., geonames=..., bbox=..., ...)   # même forme que DATASETS dans build.py
   def prepare(raw, out, h): ...            # même contrat que prepare_<id> ; h = module build (h.P, h.CC0, h.write_vcf...)
   ```
   Si un helper manque, écris-le dans ton module. Si tu trouves un bug dans `build.py`, ne le corrige pas : signale-le dans ton rapport.
2. Téléchargement : depuis `~/genovalia-injection`, `.venv/bin/python fetch.py <id>`. Articles et suppléments en accès libre dans `<id>/raw/papers/` (et `supp/`) : Europe PMC `fullTextXML` + `supplementaryFiles`, bioRxiv, éditeurs OA (voir `papers.py` pour les recettes). Ce qui échoue va dans `MANUAL` avec l'URL.
3. `.venv/bin/python build.py <id>` jusqu'à ce qu'il se termine ; lis `<id>/QC_REPORT.md` ; vérifie indépendamment ce qui surprend (règle 7 du skill).
4. `<id>/issues.json`, en français, pour l'artefact de synthèse :
   ```json
   {"id": "...", "sp": "Genre espèce", "common": "nom commun fr", "ind": "1 474", "annInd": "1 310 ou null",
    "snps": "17 436", "annSnps": "... ou null", "vcf": "Fourni (Dryad) | Converti depuis ...", "attrs": "résumé des attributs par individu",
    "ref": "génome de référence ou de novo", "src": "DOI du dépôt",
    "issues": [["ok|mit|open", 0|1, "problème", "comment il a été traité / ce qu'il faut faire"], ...]}
   ```
   Statuts : ok = Réglé, mit = Contourné, open = Ouvert ; 1 = bloquant. Phrases simples, chiffres exacts.

## Téléchargements
- Téléchargement de fichiers Dryad = authentification requise (401/403). Cherche la copie Zenodo par titre exact : `https://zenodo.org/api/records?q=title:"<titre>"&all_versions=1`, puis `https://zenodo.org/api/records/<id>/files/<nom>/content`. Sinon une copie Borealis/UBC (`https://borealisdata.ca/api/access/datafile/<id>?format=original`). Sinon `MANUAL` + problème ouvert bloquant « données à récupérer » (garde l'URL : Steve ira les chercher).
- Dryad et Zenodo renvoient 429 si on insiste : attends et réessaie (10 s, 30 s, 60 s…), jamais de boucle serrée.
- Plafond : ne télécharge pas de fichier > 2 Go ; prends le VCF filtré/analysé plutôt que les appels bruts. Si le seul fichier de génotypes dépasse 2 Go, arrête-toi pour ce jeu et signale-le.
- Mémoire limitée (plusieurs agents en parallèle) : lis les gros fichiers en flux, pas de pandas sur un fichier > 500 Mo.

## Portée
- Si, à l'inspection, le jeu n'a pas de génotypes SNP par individu (microsatellites seulement, fréquences, résultats d'analyse, séquences), ne produis rien d'autre qu'un `<id>/issues.json` avec `"vcf": "Hors périmètre"` et la raison.
- Plusieurs espèces analysées séparément dans un même dépôt = un jeu par espèce (ID de l'espèce). Hybrides/espèces mêlées dans une même analyse = un seul jeu `spp`.
- ID : ceux qui te sont assignés. Si l'espèce réelle diffère, choisis un ID selon la règle du skill, vérifie qu'il n'existe ni dans `~/genovalia-injection/` ni dans la BD (`build.existing_ids()`), et dis-le.

## Interdits
- Ne cherche ni ne lis aucun identifiant (.Renviron, .env, jetons). Pas de Pydio. Pas d'injection.
- Ne supprime rien hors de tes dossiers `<id>/` et `prep/<id>.py`.
- **N'utilise jamais l'outil Agent toi-même : ne lance pas de sous-agent imbriqué.** Traite tes jeux toi-même, un par un, séquentiellement. Un agent qui a lancé un sous-agent par jeu a déjà fait perdre tout un lot au quota d'API.

## Rapport final (ta réponse)
Court. Un tableau : ID, espèce, individus × SNPs, bloquant (oui/non, une phrase si oui). Sous le tableau, seulement les points qui ont demandé une décision (choix de fichier, fusion ou non, ID changé) et les documents à récupérer (URL). Le détail complet est déjà dans `<id>/QC_REPORT.md` et `<id>/issues.json` : ne le répète pas.

## Limites du chargeur : jamais un motif d'exclusion
Une limite connue du code de data-explorer (chargeur VCF, format de stockage des génotypes, etc.) n'est **jamais** une raison d'écarter un jeu ou de ne pas le préparer. Prépare le jeu normalement, signale la limite comme un problème (« mit » si contourné, « open » sinon), et laisse Steve décider. Ne fais pas toi-même le calcul « est-ce que le chargeur actuel saura le lire ? » pour décider si tu prépares un jeu.

## Matrices à convertir (jeux B)
- Prouve le codage avant de convertir (règle du skill) : README, article, ou recoupement avec un autre fichier du dépôt. Écris la preuve dans `decisions`.
- Pas de positions : fais comme picgla2 (CHROM = nom du marqueur, POS = 1) et signale-le.
- Allèles non nucléotidiques (genepop 01/02 ou 001/002, Arlequin, 0/1/2) : si la convention est documentée (ex. export Stacks genepop 01=A, 02=C, 03=G, 04=T ; carte/flanking avec les bases), utilise les vraies bases. Sinon garde REF = A et ALT = C comme codes de remplacement, écris la convention dans l'en-tête `##source`, et ajoute un problème « mit » : bases réelles inconnues.
- genepop/Arlequin/STRUCTURE : l'ID individuel est souvent préfixé par la population ; garde l'ID exact du fichier comme `id`, et dérive `site_code` à part.
- Fichiers Excel : lis toutes les feuilles avant de décider ; ne joins jamais par ordre de lignes.
- Jeux de quelques marqueurs seulement (gènes candidats, SNP diagnostiques, < 50 SNP) : prépare-les quand même, mais ajoute un problème « mit » sur leur faible intérêt pour data-explorer.
