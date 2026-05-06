# Outils autour de l'application

Ce repertoire regroupe les scripts utilitaires et de preparation qui accompagnent l'application.

Exemples :

- `init_db.py` : initialise la base SQLite locale
- `generate_fake_residents.py` : cree une base de demonstration avec des habitants fictifs
- `import_ban_addresses.py` : importe les adresses publiques depuis la BAN
- `refresh_building_cache.py` : regenere le cache local des batiments
- `prefetch_ortho_tiles.py` : precharge le cache des tuiles satellite
- `regenerer_habitants_realistes.bat` : lance la regeneration realiste des habitants sous Windows
- `mettre_a_jour_cache_carte.bat` : met a jour le cache local des batiments sous Windows
- `mettre_a_jour_cache_tuiles_satellite.bat` : precharge le cache satellite sous Windows
- `data/insee_first_names_weighted.json` : cache de prenoms utilise pour les donnees de demonstration
- `data/douarnenez_addresses_preview.json` : apercu JSON genere par l'import BAN
