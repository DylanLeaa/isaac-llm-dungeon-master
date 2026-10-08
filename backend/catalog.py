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
    "grab_bag": "sack of random pickups", "pill": "random pill", "card": "random card/rune",
    "trinket": "random trinket", "battery": "lil battery",
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
