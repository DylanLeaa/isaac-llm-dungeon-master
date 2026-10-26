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
    "lucky_penny": "1 coin AND +1 luck for the rest of the run (a stat up, not a coin)", "sticky_nickel": "nickel stuck to the floor (needs a bomb)",
    "bomb": "1 bomb", "double_bomb": "2 bombs", "golden_bomb": "infinite bombs for the floor",
    "troll_bomb": "LIVE bomb that explodes ~2s after spawning",
    "key": "1 key", "double_key": "2 keys", "golden_key": "infinite keys for the floor",
    "charged_key": "key + active charge",
    "red_heart": "full red heart", "half_heart": "half red heart", "soul_heart": "soul heart",
    "black_heart": "black heart", "eternal_heart": "eternal heart",
    "chest": "brown chest (1-3 pickups)", "locked_chest": "golden chest (needs a key; can hold a pedestal item)",
    "red_chest": "red chest (gamble)", "spiked_chest": "spiked chest (hurts)",
    "mimic_chest": "chest that is secretly a trap",
    "grab_bag": "sack of random pickups",
    "pill": "a pill (its colour's effect is whatever your pills deck assigned)",
    "card": "the next card/rune in your cards deck",
    "trinket": "the next trinket in your trinkets deck", "battery": "lil battery",
}
# Outcomes a payout_plan entry may use besides a reward: stiff the player, or hand out a pool item.
PAYOUT_OUTCOMES = ("nothing", "item")
# What a tinted rock may hold besides a reward: nothing, or the Small Rock item on a pedestal (if unlocked).
ROCK_OUTCOMES = ("nothing", "small_rock")
LOW_VALUE = {"penny", "half_heart", "pill"}
LUCK = {"lucky_penny": 1}                   # permanent luck each reward gives
CHESTS = {"chest", "locked_chest", "red_chest", "spiked_chest", "mimic_chest"}

# Vanilla room clears (matches the mod's VANILLA_PER_CLEAR).
VANILLA_DROP_RATE = 0.40                    # share of normal room clears that drop anything
VANILLA_PICKUPS_PER_CLEAR = 0.5             # pickups per clear, empty clears included
TRAPS = {"troll_bomb", "mimic_chest", "spiked_chest", "sticky_nickel"}
KEYS = {"key", "double_key", "golden_key", "charged_key"}
BOMBS = {"bomb", "double_bomb", "golden_bomb"}
COINS = {"penny", "double_penny", "nickel", "dime", "lucky_penny", "sticky_nickel"}
HEARTS = {"red_heart", "half_heart", "soul_heart", "black_heart", "eternal_heart"}

# ----------------------------------------------------------------- the economy (economy.py)
# The resource families every deck's outcomes are budgeted in. Units are value, not pickup count:
# coins in cents, hearts in half hearts, keys and bombs by count (a golden one is a fixed premium of 3),
# cards / pills / trinkets by count, luck in points, power in item quality (+ stat pills).
FAMILIES = ("keys", "bombs", "coins", "hearts", "cards", "pills", "trinkets", "luck", "traps", "power")
UNITS = {
    "penny": {"coins": 1}, "double_penny": {"coins": 2}, "nickel": {"coins": 5}, "dime": {"coins": 10},
    "lucky_penny": {"coins": 1, "luck": 1}, "sticky_nickel": {"coins": 5, "traps": 1},
    "bomb": {"bombs": 1}, "double_bomb": {"bombs": 2}, "golden_bomb": {"bombs": 3}, "troll_bomb": {"traps": 1},
    "key": {"keys": 1}, "double_key": {"keys": 2}, "golden_key": {"keys": 3}, "charged_key": {"keys": 1},
    "red_heart": {"hearts": 2}, "half_heart": {"hearts": 1}, "soul_heart": {"hearts": 2},
    "black_heart": {"hearts": 2}, "eternal_heart": {"hearts": 2},
    "spiked_chest": {"traps": 1}, "mimic_chest": {"traps": 1},
    "pill": {"pills": 1}, "card": {"cards": 1}, "trinket": {"trinkets": 1},
}
# What vanilla hands out per normal room (its clear, its kills and its breakables together), in the
# units above, plus a floor's fixed extras (chests, machines). A Basement floor of ~10 rooms comes to
# about 8 cents and 3 keys. First estimates: the mod's roll audit (`rolls` events) is what calibrates them.
VANILLA_PER_ROOM = {"keys": 0.22, "bombs": 0.22, "coins": 0.7, "hearts": 0.45, "cards": 0.06, "pills": 0.06,
                    "trinkets": 0.03}
VANILLA_PER_FLOOR = {"keys": 0.8, "bombs": 0.3, "coins": 1.0, "hearts": 0.5, "cards": 0.3, "pills": 0.3,
                     "trinkets": 0.2, "power": 4.0}
VANILLA_LUCK_PER_RUN = 1.0
# The floor brief's word for a family -> a multiplier on vanilla.
ECONOMY_WORDS = {"starve": 0.3, "lean": 0.6, "vanilla": 1.0, "generous": 1.5, "flood": 2.2}
# How far over budget a deliberate gift may go, in each family's units (scaled down by cruelty).
SLACK = {"keys": 1, "bombs": 1, "coins": 3, "hearts": 2, "cards": 1, "pills": 1, "trinkets": 1, "luck": 0,
         "traps": 0, "power": 2}
# Shop value in cents, for a machine's house edge (what it pays back per payment). "item" is a jackpot
# outside the edge (one per schedule at most).
SHOP_VALUE = {"penny": 1, "double_penny": 2, "nickel": 5, "dime": 10, "lucky_penny": 5, "sticky_nickel": 4,
              "bomb": 5, "double_bomb": 10, "golden_bomb": 15, "troll_bomb": 0,
              "key": 5, "double_key": 10, "golden_key": 15, "charged_key": 7,
              "red_heart": 3, "half_heart": 2, "soul_heart": 5, "black_heart": 6, "eternal_heart": 8,
              "chest": 6, "locked_chest": 8, "red_chest": 5, "spiked_chest": 3, "mimic_chest": 0, "grab_bag": 7,
              "pill": 5, "card": 5, "trinket": 6, "battery": 5, "nothing": 0}
# What one use of a machine costs the player, in cents (a heart-paid machine costs a red heart ~ 3c).
PAYMENT = {"beggar": 1, "demon_beggar": 3, "shell_game": 1, "key_master": 5, "bomb_bum": 5, "battery_bum": 1,
           "crane_game": 5, "rotten_beggar": 1, "slot_machine": 1, "hell_game": 3}
# Pill effects that move a stat: they count as power (+1 good, -1 bad).
STAT_PILLS = {"health_up": 1, "range_up": 1, "speed_up": 1, "tears_up": 1, "luck_up": 1, "shot_speed_up": 1,
              "health_down": -1, "range_down": -1, "speed_down": -1, "tears_down": -1, "luck_down": -1,
              "shot_speed_down": -1}

# What happens to pickups that are already part of the room layout.
LAYOUT_PICKUP_MODES = {
    "keep": "leave layout pickups be: in a room whose clear you decide they are held with its drops, and you choose what is put back",
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

# Champion colours the DM may force (ChampionColor.* enum names). Only behaviours, not drops: a
# champion's kill drops from the drops deck like any other.
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
