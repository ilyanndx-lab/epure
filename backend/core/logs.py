"""Masquage des secrets dans les journaux — IMPÉRATIF CLAUDE.md §6.

« Le token d'API ne sort jamais — ni de ``GET /instance/config``, ni des logs,
ni d'un message d'erreur. » Les deux premières moitiés étaient tenues ; la
troisième fuyait, et par un chemin qu'aucune relecture du code d'Épure ne
pouvait montrer, puisque la ligne est écrite par uvicorn :

    INFO: 127.0.0.1:51320 - "WebSocket /ws/chat?token=YGdSbkNxz4jKf-sll5zDefw5zOeLBGds4ac5qbiSINk" [accepted]

Le token voyage en query param parce qu'un ``new WebSocket()`` n'accepte pas
d'en-tête (cf. ``core.auth.ws_require_token``) — ce n'est pas discutable côté
navigateur. Or uvicorn journalise le chemin **avec** sa query, à deux endroits :

    uvicorn.error    '%s - "WebSocket %s" [accepted]'    (et 403, et %d)
    uvicorn.access   '%s - "%s %s HTTP/%s" %d'

Ce qui a rendu la fuite urgente, c'est l'empaquetage
(``docs/distribution-empaquetee.md``) : jusqu'ici le journal restait sur le
poste de son propriétaire, qui connaît déjà son token. Dans un paquet distribué
il devient un fichier sur le disque de quelqu'un d'autre, recopié dans un
message quand quelque chose ne marche pas — et le token d'Épure ne périme pas.

**Pourquoi un filtre de logging et pas un réglage d'uvicorn.** Il n'existe pas
d'option pour retirer la query de la ligne d'accès ; les seules alternatives
seraient de couper le log d'accès (on perd un outil de diagnostic) ou de
remplacer le token par un sous-protocole WebSocket (un vrai changement de
protocole, à faire un jour, mais qui ne protège pas les journaux déjà écrits).

**Pourquoi mutiler ``record.args`` et pas formater le message.** Un filtre qui
renverrait un texte déjà formaté imposerait son format à tous les handlers.
En remplaçant les arguments, la ligne reste celle d'uvicorn, avec ses couleurs
et son alignement — seule la valeur du secret change. Et le masquage vaut pour
tous les handlers, y compris ceux posés après.

Ce module ne prétend pas être une frontière de sécurité : c'est un garde-fou de
journalisation. Il masque ce qu'il reconnaît (``token``, ``api_key``), pas ce
qu'il n'a jamais vu passer.
"""

import logging
import re

#: Ce qui remplace la valeur. Assez visible pour qu'on comprenne, en lisant un
#: journal, qu'il y avait bien un paramètre et qu'il a été masqué — un simple
#: retrait laisserait croire à une requête sans token, c'est-à-dire à un bug.
MASQUE = "***masqué***"

#: Paramètres de query dont la valeur est un secret. `access_token` et
#: `apikey` sont couverts par les alternatives sans ancrage à gauche.
_SECRET_RE = re.compile(r"(?i)((?:token|api_?key)=)([^&\s\"'<>]+)")

#: Loggers à filtrer. Ceux d'uvicorn ont leurs PROPRES handlers et
#: ``propagate = False`` : ils échappent au ``basicConfig(force=True)`` de
#: ``main`` comme à tout filtre posé sur la racine. Il faut donc les nommer.
#: La racine est incluse par ceinture et bretelles — pour le jour où du code
#: d'Épure journalisera une URL construite ailleurs.
LOGGERS_FILTRES = ("uvicorn.access", "uvicorn.error", "")


def masquer(texte: str) -> str:
    """Remplace la valeur des paramètres secrets d'une query par :data:`MASQUE`.

    Volontairement tolérant sur la forme : le paramètre peut être en début de
    query (``?token=…``), au milieu (``&token=…``) ou dans un fragment de texte
    libre. La valeur s'arrête au premier ``&``, espace, guillemet ou chevron,
    parce que ces lignes de journal encadrent le chemin de guillemets.
    """
    return _SECRET_RE.sub(lambda m: m.group(1) + MASQUE, texte)


class FiltreSecrets(logging.Filter):
    """Masque les secrets dans le message ET dans les arguments d'un record.

    Ne filtre RIEN au sens de « laisser passer » : renvoie toujours ``True``.
    C'est un transformateur déguisé en filtre, parce que c'est le seul point
    d'accroche que la bibliothèque standard offre avant le formatage.

    Les arguments non textuels (adresse client, code de statut) sont laissés
    tels quels : les toucher casserait le ``%d`` du format d'uvicorn.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and "=" in record.msg:
            record.msg = masquer(record.msg)
        args = record.args
        if isinstance(args, dict):
            record.args = {
                clef: masquer(v) if isinstance(v, str) else v for clef, v in args.items()
            }
        elif isinstance(args, tuple):
            record.args = tuple(masquer(a) if isinstance(a, str) else a for a in args)
        return True


def masquer_secrets_dans_logs(noms=LOGGERS_FILTRES) -> list[str]:
    """Installe :class:`FiltreSecrets` sur les loggers concernés. Idempotent.

    Idempotent parce que ``main`` peut être importé plusieurs fois dans un même
    process (la suite de tests, ``uvicorn --reload``), et qu'empiler dix fois le
    même filtre ferait dix passes de regex par ligne de journal.

    Renvoie les noms des loggers effectivement équipés — utile en test, et pour
    que l'appelant puisse le journaliser.
    """
    poses = []
    for nom in noms:
        logger = logging.getLogger(nom)
        if any(isinstance(f, FiltreSecrets) for f in logger.filters):
            continue
        logger.addFilter(FiltreSecrets())
        poses.append(nom or "<racine>")
    return poses
