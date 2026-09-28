#!/usr/bin/env bash
# Install or update every skill in this repo for Claude Code, in one step.
set -e
REPO=~/genovalia-skills

if [ -d "$REPO" ]; then
  echo "Mise à jour..."
  git -C "$REPO" pull -q
else
  echo "Installation..."
  git clone -q git@github.com:genovalia/genovalia-skills.git "$REPO"
fi

mkdir -p ~/.claude/skills
for dir in "$REPO"/skills/*/; do
  name=$(basename "$dir")
  ln -sfn "$dir" ~/.claude/skills/"$name"
  echo "  ✓ $name"
done

echo "Prêt. Ouvre Claude Code dans n'importe quel projet : les skills ci-dessus y sont disponibles."
