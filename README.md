# genovalia-skills

Claude Code skills partagés entre les projets Genovalia. Un skill par sous-dossier de `skills/` : conventions, scripts et leçons apprises, indépendants de tout dépôt de code produit.

## Skills disponibles

- [`genotyping-curation`](skills/genotyping-curation/SKILL.md) — préparation et validation des jeux de génotypage publics (DCAT, OCA/Semantic Engine, VCF, CSV data-explorer).
- [`openshift-deploy`](skills/openshift-deploy/SKILL.md) — standard de déploiement OpenShift (manifestes `oc/dev`, `oc/prod`, RBAC du compte CI, `deploy.py`), workflows GitHub et variables/secrets des GitHub Environments : scaffolding d'un nouveau projet et audit d'un dépôt existant.

## Installation locale (Claude Code)

**En une commande**, à coller dans un terminal :

```bash
curl -fsSL https://raw.githubusercontent.com/genovalia/genovalia-skills/master/install.sh | bash
```

Ça télécharge tous les skills de ce dépôt et les rend disponibles dans **n'importe quel projet** ouvert avec Claude Code sur cette machine. Pour vérifier : ouvrir Claude Code dans un projet et voir si le skill apparaît dans sa liste au démarrage.

Pour mettre à jour plus tard : relancer exactement la même commande.

<details>
<summary>Ce que fait la commande, en détail (si tu préfères le faire main dans la main)</summary>

```bash
git clone git@github.com:genovalia/genovalia-skills.git ~/genovalia-skills   # ou : cd ~/genovalia-skills && git pull, si déjà cloné

mkdir -p ~/.claude/skills
ln -s ~/genovalia-skills/skills/genotyping-curation ~/.claude/skills/genotyping-curation
```

Le lien symbolique pointe toujours vers la version à jour du clone : un `git pull` dans `~/genovalia-skills` suffit ensuite, pas besoin de refaire le lien.
</details>

## Installation dans Claude Desktop

Claude Desktop (et claude.ai) utilise le même format `SKILL.md` que Claude Code, mais l'installe via l'interface plutôt qu'un lien symbolique : pas d'accès au système de fichiers local, donc on lui fournit une archive.

```bash
cd ~/genovalia-skills/skills/genotyping-curation
zip -r /tmp/genotyping-curation.zip .
```

Puis dans Claude Desktop : **Réglages → Capacités (Capabilities) → Skills → Charger un skill (Upload skill)**, sélectionner `/tmp/genotyping-curation.zip`. Le libellé exact du menu peut varier selon la version de l'application ; chercher « Skills » dans les réglages si ce chemin a changé.

Mise à jour : refaire le zip après un `git pull` dans `~/genovalia-skills`, puis re-charger — Claude Desktop remplace le skill existant du même nom.

## Ajouter un nouveau skill

1. `skills/<nom-du-skill>/SKILL.md` + ressources (`references/`, `scripts/`).
2. Ajouter une ligne dans la section « Skills disponibles » ci-dessus.
3. Chaque personne qui l'utilise fait un `ln -s` supplémentaire (voir Installation locale) après son prochain `git pull`.
