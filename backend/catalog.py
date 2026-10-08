"""Static catalogs the Dungeon Master is allowed to choose from.

Enemy entries use the Isaac Lua enum *names* (EntityType.ENTITY_*), never raw
numbers, so the mod resolves them with the game's own enums and silently skips
anything that does not exist in the player's game version.
"""

# key -> spec. "threat" is a rough 1-3 nuisance score used for budget clamping.
ENEMIES = {
    "attack_fly":  {"etype": "ENTITY_ATTACKFLY", "variant": 0, "name": "Attack Fly",  "threat": 1, "note": "cheap chip damage"},
    "pooter":      {"etype": "ENTITY_POOTER",    "variant": 0, "name": "Pooter",      "threat": 1, "note": "slow shots from range"},
    "dip":         {"etype": "ENTITY_DIP",       "variant": 0, "name": "Dip",         "threat": 1, "note": "harmless-looking, bumps you"},
    "gaper":       {"etype": "ENTITY_GAPER",     "variant": 1, "name": "Gaper",       "threat": 1, "note": "walks at you"},
    "maggot":      {"etype": "ENTITY_MAGGOT",    "variant": 0, "name": "Maggot",      "threat": 1, "note": "crawls"},
    "spider":      {"etype": "ENTITY_SPIDER",    "variant": 0, "name": "Spider",      "threat": 1, "note": "erratic, fast"},
    "clotty":      {"etype": "ENTITY_CLOTTY",    "variant": 0, "name": "Clotty",      "threat": 2, "note": "4-way shots"},
    "hopper":      {"etype": "ENTITY_HOPPER",    "variant": 0, "name": "Hopper",      "threat": 2, "note": "jumpy, hard to hit"},
    "charger":     {"etype": "ENTITY_CHARGER",   "variant": 0, "name": "Charger",     "threat": 2, "note": "charges in straight lines"},
    "mulligan":    {"etype": "ENTITY_MULLIGAN",  "variant": 0, "name": "Mulligan",    "threat": 2, "note": "bursts into flies on death"},
    "maw":         {"etype": "ENTITY_MAW",       "variant": 0, "name": "Maw",         "threat": 2, "note": "floats, shoots"},
    "spitty":      {"etype": "ENTITY_SPITTY",    "variant": 0, "name": "Spitty",      "threat": 2, "note": "aligns and spits"},
    "big_spider":  {"etype": "ENTITY_BIGSPIDER", "variant": 0, "name": "Big Spider",  "threat": 2, "note": "splits into spiders"},
    "boom_fly":    {"etype": "ENTITY_BOOMFLY",   "variant": 0, "name": "Boom Fly",    "threat": 3, "note": "EXPLODES when killed - great near pickups"},
    "host":        {"etype": "ENTITY_HOST",      "variant": 0, "name": "Host",        "threat": 3, "note": "invulnerable until it opens"},
    "knight":      {"etype": "ENTITY_KNIGHT",    "variant": 0, "name": "Knight",      "threat": 3, "note": "only hittable from behind"},
    "globin":      {"etype": "ENTITY_GLOBIN",    "variant": 0, "name": "Globin",      "threat": 3, "note": "revives unless finished off"},
    "sucker":      {"etype": "ENTITY_SUCKER",    "variant": 0, "name": "Sucker",      "threat": 2, "note": "flies, splits shots on death"},
}

# Reward names the mod knows how to spawn (see mod dm/apply.lua REWARDS table).
REWARDS = {
    "penny": "1 coin", "double_penny": "2 coins", "nickel": "5 coins", "dime": "10 coins",
    "lucky_penny": "coin +1 luck", "sticky_nickel": "nickel stuck to the floor (needs a bomb)",
    "bomb": "1 bomb", "double_bomb": "2 bombs", "golden_bomb": "infinite bombs for the floor",
    "troll_bomb": "LIVE bomb that explodes ~2s after spawning",
    "key": "1 key", "double_key": "2 keys", "golden_key": "infinite keys for the floor",
    "charged_key": "key + active charge",
    "red_heart": "full red heart", "half_heart": "half red heart", "soul_heart": "soul heart",
    "black_heart": "black heart", "eternal_heart": "eternal heart",
    "chest": "brown chest", "locked_chest": "golden chest (needs a key)",
    "red_chest": "red chest (gamble)", "spiked_chest": "spiked chest (hurts)",
    "mimic_chest": "chest that is secretly a trap",
    "grab_bag": "sack of random pickups",
    "pill": "a pill (its colour's effect is whatever you assigned in your consumables plan)",
    "card": "the next card/rune in your consumables plan",
    "trinket": "the next trinket in your consumables plan", "battery": "lil battery",
}
LOW_VALUE = {"penny", "half_heart", "pill"}
TRAPS = {"troll_bomb", "mimic_chest", "spiked_chest", "sticky_nickel"}
KEYS = {"key", "double_key", "golden_key", "charged_key"}
BOMBS = {"bomb", "double_bomb", "golden_bomb"}
COINS = {"penny", "double_penny", "nickel", "dime", "lucky_penny", "sticky_nickel"}
HEARTS = {"red_heart", "half_heart", "soul_heart", "black_heart", "eternal_heart"}

# What happens to pickups that are already part of the room layout.
LAYOUT_PICKUP_MODES = {
    "keep": "leave layout pickups alone",
    "remove": "delete every layout pickup",
    "to_pennies": "turn every layout pickup into a single penny",
    "to_bombs": "turn every layout pickup into bombs",
    "to_keys": "turn every layout pickup into keys",
    "to_hearts": "turn every layout pickup into half hearts",
    "troll_bombs": "replace ONE layout pickup with a live troll bomb",
}

# Curses the DM may add at floor generation. Labyrinth/Giant are excluded on
# purpose: they need the level generator to run with them from the start.
CURSES = {
    "darkness": "Curse of Darkness - room is dark",
    "lost": "Curse of the Lost - no map",
    "unknown": "Curse of the Unknown - health hidden",
    "maze": "Curse of the Maze - doors sometimes lead to the wrong room",
    "blind": "Curse of the Blind - items show as '?'",
}

TREASURE_DISTANCE = ["any", "near", "far"]

# Champion colours the DM may force (ChampionColor.* enum names). Only behaviours, not drops:
# every colour has its own drop, and the game handles it.
CHAMPION_COLORS = {
    "red": ("CHAMPION_RED", "double HP"),
    "yellow": ("CHAMPION_YELLOW", "faster"),
    "dark_red": ("CHAMPION_DARK_RED", "collapses into a meat pile that revives unless killed again"),
    "light_blue": ("CHAMPION_LIGHT_BLUE", "bursts into a ring of shots on death"),
    "purple": ("CHAMPION_PURPLE", "drags you and your tears towards it"),
    "camo": ("CHAMPION_CAMO", "nearly invisible"),
    "pulse_green": ("CHAMPION_PULSE_GREEN", "regenerates health"),
    "fly_protected": ("CHAMPION_FLY_PROTECTED", "shielded by orbiting flies"),
    "tiny": ("CHAMPION_TINY", "small, fast, fragile"),
    "giant": ("CHAMPION_GIANT", "huge and tanky"),
}

# Cards and runes the DM may hand out (Card.* enum names). "cat" decides where the game allows
# a card: rune-only draws take runes, some draws exclude runes or playing/special cards.
# Suicide King is left out on purpose: it is an instant death with no counterplay.
CARDS = {
    "fool":              ("CARD_FOOL", "tarot", "teleports you back to the start room"),
    "magician":          ("CARD_MAGICIAN", "tarot", "homing tears for the room"),
    "high_priestess":    ("CARD_HIGH_PRIESTESS", "tarot", "Mom's foot stomps an enemy"),
    "empress":           ("CARD_EMPRESS", "tarot", "damage + speed up for the room"),
    "emperor":           ("CARD_EMPEROR", "tarot", "teleports you to the boss room"),
    "hierophant":        ("CARD_HIEROPHANT", "tarot", "2 soul hearts"),
    "lovers":            ("CARD_LOVERS", "tarot", "2 red hearts"),
    "chariot":           ("CARD_CHARIOT", "tarot", "invincible charge for a few seconds"),
    "justice":           ("CARD_JUSTICE", "tarot", "1 coin, 1 bomb, 1 key, 1 heart"),
    "hermit":            ("CARD_HERMIT", "tarot", "teleports you to the shop"),
    "wheel_of_fortune":  ("CARD_WHEEL_OF_FORTUNE", "tarot", "spawns a slot or fortune machine"),
    "strength":          ("CARD_STRENGTH", "tarot", "stat up + temporary heart for the room"),
    "hanged_man":        ("CARD_HANGED_MAN", "tarot", "flight for the room"),
    "death":             ("CARD_DEATH", "tarot", "damages every enemy in the room"),
    "temperance":        ("CARD_TEMPERANCE", "tarot", "spawns a blood donation machine"),
    "devil":             ("CARD_DEVIL", "tarot", "+damage for the room"),
    "tower":             ("CARD_TOWER", "tarot", "6 TROLL BOMBS around the room"),
    "stars":             ("CARD_STARS", "tarot", "teleports you to the treasure room"),
    "moon":              ("CARD_MOON", "tarot", "teleports you to the secret room"),
    "sun":               ("CARD_SUN", "tarot", "full heal, damages enemies, reveals the map"),
    "judgement":         ("CARD_JUDGEMENT", "tarot", "spawns a beggar"),
    "world":             ("CARD_WORLD", "tarot", "reveals the map"),
    "two_of_clubs":      ("CARD_CLUBS_2", "playing", "doubles your bombs"),
    "two_of_diamonds":   ("CARD_DIAMONDS_2", "playing", "doubles your coins"),
    "two_of_spades":     ("CARD_SPADES_2", "playing", "doubles your keys"),
    "two_of_hearts":     ("CARD_HEARTS_2", "playing", "doubles your red hearts"),
    "ace_of_spades":     ("CARD_ACE_OF_SPADES", "playing", "turns pickups and enemies into keys"),
    "ace_of_diamonds":   ("CARD_ACE_OF_DIAMONDS", "playing", "turns pickups and enemies into coins"),
    "joker":             ("CARD_JOKER", "playing", "teleports you to the devil/angel room"),
    "chaos_card":        ("CARD_CHAOS", "special", "a thrown card that kills anything it hits"),
    "credit_card":       ("CARD_CREDIT", "special", "everything in the shop is free"),
    "rules_card":        ("CARD_RULES", "special", "shows a useless hint"),
    "get_out_of_jail":   ("CARD_GET_OUT_OF_JAIL", "special", "opens every door in the room"),
    "question_mark_card": ("CARD_QUESTIONMARK", "special", "uses your active item for free"),
    "dice_shard":        ("CARD_DICE_SHARD", "special", "rerolls items and pickups in the room"),
    "holy_card":         ("CARD_HOLY", "special", "blocks one hit"),
    "reverse_fool":      ("CARD_REVERSE_FOOL", "reverse", "spills your coins, bombs and keys onto the floor"),
    "reverse_high_priestess": ("CARD_REVERSE_HIGH_PRIESTESS", "reverse", "Mom's foot stomps near YOU for a minute"),
    "reverse_hermit":    ("CARD_REVERSE_HERMIT", "reverse", "turns every pickup and item in the room into coins"),
    "reverse_justice":   ("CARD_REVERSE_JUSTICE", "reverse", "spawns golden chests"),
    "reverse_temperance": ("CARD_REVERSE_TEMPERANCE", "reverse", "forces you to eat 5 random pills"),
    "reverse_tower":     ("CARD_REVERSE_TOWER", "reverse", "fills the room with rocks (some tinted)"),
    "reverse_moon":      ("CARD_REVERSE_MOON", "reverse", "teleports you to the ultra secret room"),
    "rune_hagalaz":      ("RUNE_HAGALAZ", "rune", "destroys every rock in the room"),
    "rune_jera":         ("RUNE_JERA", "rune", "duplicates the pickups in the room"),
    "rune_ehwaz":        ("RUNE_EHWAZ", "rune", "opens a crawlspace"),
    "rune_dagaz":        ("RUNE_DAGAZ", "rune", "removes curses + 1 soul heart"),
    "rune_ansuz":        ("RUNE_ANSUZ", "rune", "reveals the whole map"),
    "rune_perthro":      ("RUNE_PERTHRO", "rune", "rerolls the pedestals in the room"),
    "rune_berkano":      ("RUNE_BERKANO", "rune", "spawns blue flies and spiders"),
    "rune_algiz":        ("RUNE_ALGIZ", "rune", "invincible for a while"),
    "rune_blank":        ("RUNE_BLANK", "rune", "a random rune's effect"),
    "rune_black":        ("RUNE_BLACK", "rune", "damages enemies, turns pickups into flies, items into stats"),
}

# Pill effects, keyed by the PillEffect.PILLEFFECT_* suffix in lower case.
# kind: good / bad / neutral. "counter" is the effect PHD (bad->good) or False PHD (good->bad)
# turns it into; it approximates the game's own table, which overriding pills would bypass.
PILLS = {
    "bad_gas": ("neutral", "poison fart", None),
    "bad_trip": ("bad", "1 heart of damage", "balls_of_steel"),
    "balls_of_steel": ("good", "2 soul hearts", "bad_trip"),
    "bombs_are_keys": ("neutral", "swaps bomb and key counts", None),
    "explosive_diarrhea": ("neutral", "a trail of lit bombs behind you", None),
    "full_health": ("good", "full heal", None),
    "health_down": ("bad", "-1 heart container", "health_up"),
    "health_up": ("good", "+1 heart container", "health_down"),
    "i_found_pills": ("neutral", "cosmetic only", None),
    "puberty": ("neutral", "cosmetic only", None),
    "pretty_fly": ("good", "an orbiting fly shield", None),
    "range_down": ("bad", "range down", "range_up"),
    "range_up": ("good", "range up", "range_down"),
    "speed_down": ("bad", "speed down", "speed_up"),
    "speed_up": ("good", "speed up", "speed_down"),
    "tears_down": ("bad", "fire rate down", "tears_up"),
    "tears_up": ("good", "fire rate up", "tears_down"),
    "luck_down": ("bad", "luck down", "luck_up"),
    "luck_up": ("good", "luck up", "luck_down"),
    "telepills": ("neutral", "teleports you to a random room", "questionmark"),
    "48hour_energy": ("good", "battery charge", None),
    "hematemesis": ("neutral", "drops to half a heart, spawns hearts", None),
    "paralysis": ("bad", "can't move for a few seconds", "pheromones"),
    "see_forever": ("good", "reveals secret rooms", "amnesia"),
    "pheromones": ("good", "charms enemies", "paralysis"),
    "amnesia": ("bad", "hides the map", "see_forever"),
    "lemon_party": ("neutral", "a pool of damaging creep", None),
    "wizard": ("bad", "tears fire diagonally", "power"),
    "percs": ("good", "take half damage for a while", "addicted"),
    "addicted": ("bad", "take double damage for a while", "percs"),
    "relax": ("neutral", "you poop as you walk", None),
    "questionmark": ("bad", "screen goes wobbly", "telepills"),
    "larger": ("neutral", "you grow", None),
    "smaller": ("neutral", "you shrink (smaller hitbox)", None),
    "infested_exclamation": ("neutral", "spiders out of the poops", None),
    "infested_question": ("neutral", "spiders out of the enemies", None),
    "power": ("good", "invincible stomping for a few seconds", "wizard"),
    "retro_vision": ("bad", "screen goes pixelated", "see_forever"),
    "friends_till_the_end": ("good", "blue flies", None),
    "x_lax": ("bad", "slippery creep under you", "somethings_wrong"),
    "somethings_wrong": ("neutral", "slowing creep under you", None),
    "im_drowsy": ("bad", "slows everything, you included", "im_excited"),
    "im_excited": ("good", "speeds everything up", "im_drowsy"),
    "gulp": ("good", "swallows your trinket for good", None),
    "horf": ("good", "a single huge tear", None),
    "sunshine": ("good", "invincibility for a few seconds", None),
    "vurp": ("good", "spawns the last pill you took", None),
    "shot_speed_down": ("bad", "shot speed down", "shot_speed_up"),
    "shot_speed_up": ("good", "shot speed up", "shot_speed_down"),
    "experimental": ("neutral", "one stat up, another down", None),
}
