# Suivi de distribution de sacs poubelle

Prototype local pour suivre la distribution de sacs poubelle dans une commune comme Douarnenez.

## Ce que contient cette premiere version

- une application web Python sans dependance obligatoire cote serveur ;
- une carte HTML interactive avec zoom, panning et selection visuelle de batiments ;
- un fond cartographique public IGN avec surcouche parcellaire cadastrale ;
- une base SQLite scriptable avec habitants fictifs ;
- une recherche par nom, prenom ou adresse ;
- un formulaire simple pour mettre a jour le nombre de sacs noirs et jaunes recus ;
- un script pour importer plus tard les vraies adresses publiques de Douarnenez depuis la BAN ;
- un script pour regenerer des habitants fictifs a partir des adresses disponibles.

## Lancer le projet

1. Initialiser la base locale :

```powershell
python outils/init_db.py
```

2. Demarrer le serveur local :

```powershell
python app.py
```

Alternative Windows :

```powershell
.\lancer_application.bat
```

3. Ouvrir ensuite :

```text
http://127.0.0.1:8000
```

## Generer un executable Windows

Le projet peut etre empaquete en `.exe` avec PyInstaller :

```powershell
pyinstaller suivi_distribution_sacs.spec
```

Le build s'appuie sur :

- `launcher.py` comme point d'entree de l'application a empaqueter ;
- `runtime_paths.py` pour resoudre les chemins en mode source ou en mode executable ;
- `suivi_distribution_sacs.spec` pour declarer les ressources embarquees.

L'executable genere se trouve ensuite dans :

```text
dist\SuiviDistributionSacs.exe
```

Au premier lancement, l'executable cree son propre dossier `data` a cote du `.exe`, initialise la base si besoin, puis ouvre l'application dans le navigateur.

## Outils utiles

Regenerer des habitants fictifs :

```powershell
python outils/generate_fake_residents.py --force --households 12
```

Importer les adresses publiques BAN de Douarnenez :

```powershell
python outils/import_ban_addresses.py --city-code 29046 --department 29 --city-name Douarnenez
```

Le script d'import enregistre aussi un apercu JSON dans `outils/data/douarnenez_addresses_preview.json`.

## Architecture rapide

- `app.py` : serveur HTTP local et API JSON
- `database.py` : schema SQLite, donnees de demo et fonctions de seed
- `launcher.py` : point d'entree minimal pour le lancement et l'empaquetage
- `runtime_paths.py` : gestion des chemins en mode developpement et PyInstaller
- `static/` : interface HTML/CSS/JS
- `data/` : donnees de reference utilisees directement par l'application
- `outils/` : initialisation, import BAN, generation d'habitants fictifs et scripts utilitaires
- `outils/data/` : caches, apercus et jeux de donnees lies aux scripts d'outillage

## Fichiers generes localement

Ne sont pas pousses sur GitHub :

- `build/` et `dist/` pour les builds PyInstaller ;
- les bases SQLite locales dans `data/` ;
- les caches de tuiles, profils headless Chrome et dossiers `__pycache__/`.

## Notes sur les donnees publiques

La carte utilise les services publics de l'IGN quand ils repondent :

- Plan IGN WMTS
- Parcellaire cadastral WMTS
- Batiments via WFS GeoPlateforme

Si les services cartographiques distants sont indisponibles, l'application bascule sur un mode local de demonstration avec des batiments simplifies.

## Sources publiques retenues

- La Base Adresse Nationale est le referentiel officiel des adresses : https://adresse.data.gouv.fr/decouvrir-la-BAN
- La page commune Douarnenez BAN : https://adresse.data.gouv.fr/commune/29046
- Le service de geocodage GeoPlateforme : https://cartes.gouv.fr/aide/fr/guides-utilisateur/utiliser-les-services-de-la-geoplateforme/geocodage/
- Les services WMTS GeoPlateforme : https://cartes.gouv.fr/aide/fr/guides-utilisateur/utiliser-les-services-de-la-geoplateforme/diffusion/wmts/
- Les services WFS GeoPlateforme : https://cartes.gouv.fr/aide/fr/guides-utilisateur/utiliser-les-services-de-la-geoplateforme/diffusion/wfs/

## Suite logique

- importer toutes les adresses de Douarnenez via la BAN ;
- ajouter un vrai annuaire des voies et adresses dans l'interface ;
- generer automatiquement des familles fictives a partir de ces adresses ;
- lier chaque foyer a un batiment reel ou a une parcelle ;
- ajouter un historique de distributions par campagne.
