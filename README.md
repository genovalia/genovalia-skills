# genovalia-skills

Claude Code skills partagés entre les projets Genovalia (`ulaval-recherche`). Un skill par sous-dossier de `skills/` : conventions, scripts et leçons apprises, indépendants de tout dépôt de code produit.

## Skills disponibles

- [`genotyping-curation`](skills/genotyping-curation/SKILL.md) — préparation et validation des jeux de génotypage publics (DCAT, OCA/Semantic Engine, VCF, CSV data-explorer).

## Installation locale

Un skill de ce dépôt devient disponible dans Claude Code par un lien symbolique vers `~/.claude/skills/`, ce qui le rend utilisable dans **tous** les projets ouverts sur la machine, peu importe le dépôt de code courant.

```bash
git clone git@github.com:ulaval-recherche/genovalia-skills.git ~/genovalia-skills

mkdir -p ~/.claude/skills
ln -s ~/genovalia-skills/skills/genotyping-curation ~/.claude/skills/genotyping-curation
```

Répéter le `ln -s` pour chaque nouveau skill ajouté à ce dépôt.

### Mise à jour

```bash
cd ~/genovalia-skills && git pull
```

Le lien symbolique pointe toujours vers la version à jour du clone : aucune autre étape n'est nécessaire.

### Vérifier que Claude Code le voit

Ouvrir Claude Code dans n'importe quel projet et lancer `/skill-doctor` (ou vérifier que le skill apparaît dans la liste des skills disponibles au démarrage de la session).

## Ajouter un nouveau skill

1. `skills/<nom-du-skill>/SKILL.md` + ressources (`references/`, `scripts/`).
2. Ajouter une ligne dans la section « Skills disponibles » ci-dessus.
3. Chaque personne qui l'utilise fait un `ln -s` supplémentaire (voir Installation locale) après son prochain `git pull`.
