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

1. Initialiser la base :

```powershell
python scripts/init_db.py
```

2. Demarrer le serveur local :

```powershell
python app.py
```

3. Ouvrir ensuite :

```text
http://127.0.0.1:8000
```

## Scripts utiles

Regenerer des habitants fictifs :

```powershell
python scripts/generate_fake_residents.py --force --households 12
```

Importer les adresses publiques BAN de Douarnenez :

```powershell
python scripts/import_ban_addresses.py --city-code 29046 --department 29 --city-name Douarnenez
```

Le script d'import enregistre aussi un apercu JSON dans `data/douarnenez_addresses_preview.json`.

## Architecture rapide

- `app.py` : serveur HTTP local et API JSON
- `database.py` : schema SQLite, donnees de demo et fonctions de seed
- `static/` : interface HTML/CSS/JS
- `scripts/` : initialisation, import BAN, generation d'habitants fictifs

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
