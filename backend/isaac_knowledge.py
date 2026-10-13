"""Binding of Isaac: Repentance gameplay knowledge, injected into every agent's prompt.

Compiled from the community wiki (bindingofisaacrebirth.wiki.gg): Rooms, Devil Room, Shop, Room Clear
Awards, Hearts, Luck, Curses, Pills, Blue Womb, Mega Satan, Womb and the character pages. Vanilla facts
only; what the DM is allowed to change lives in prompts.py and schemas.clamp(). ISAAC_KNOWLEDGE goes to
the DM and both planners; GATE_KNOWLEDGE is the short form for the re-ask gate, which has to stay fast.
"""

ISAAC_KNOWLEDGE = """ISAAC KNOWLEDGE (vanilla Repentance: what the player knows, expects and plays around)

THE GAME IN FRONT OF YOU BEATS GENERAL KNOWLEDGE
- Everything below is general knowledge of vanilla Repentance with everything unlocked. Any one
  run can differ from it, and where they disagree the snapshot and context are right and this
  is wrong. Common ways a run differs:
  - Unlocks: a save may lack a character's starting items, many pool items, characters, alt
    floors, rooms or whole late chapters.
  - The build: items taken, lost, rerolled or swapped change what a character has and needs.
    A character's usual loadout is at most where they started.
  - The floor: which special rooms, curses and locked doors exist is in `level`, not in the
    general "every floor has..." rules.
  - The mode: snapshot.difficulty (0 normal, 1 hard, 2-3 Greed), challenges and other mods
    change rules, drops and what's on offer.
- So read the specific context before applying a general rule. What they hold is exactly
  player.* ("none" means none), what is on offer is exactly the candidates, what the floor has
  is exactly `level`. Never build a plan, gag or taunt on something the snapshot doesn't show,
  however standard it is in vanilla. When you are unsure, plan around what you can see.
- snapshot.save.items_unlocked / items_total hints at how far this save has got. Well under half
  unlocked is a newer save that may not reach the later chapters: keep those beats loose.

FLOORS (snapshot level.stage; stage_type picks the variant)
- 1-2 Basement/Cellar/Burning Basement (alt: Downpour/Dross), 3-4 Caves/Catacombs/Flooded Caves
  (alt: Mines/Ashpit), 5-6 Depths/Necropolis/Dank Depths (alt: Mausoleum/Gehenna; Mom on floor 6),
  7-8 Womb/Utero/Scarred Womb (alt: Corpse; Mom's Heart / It Lives on 8), 9 Blue Womb (Hush),
  10 Sheol (Satan) / Cathedral (Isaac), 11 Dark Room (The Lamb) / Chest (???), 12 The Void (Delirium),
  13 Home (The Beast). Mega Satan sits behind the golden gate on floor 11.
- From the Womb on, EVERY hit costs a full heart, whatever the source. One heart there is one hit
  from death, so mercy thresholds bite earlier.
- From the Womb on, treasure rooms and shops don't generate naturally. Pickups there are the
  player's only supply line, and coins have little to buy.
- Timers: beat Mom in under 20:00 and the Boss Rush opens; beat Mom's Heart / It Lives in under
  30:00 and the Blue Womb (Hush) opens. Extra enemies and champions cost time, so near these
  marks a slow room is a troll aimed at the clock, not the health bar.
- Alt path: Downpour/Dross (through a mirror) and Mines/Ashpit each hide a knife piece; both
  together open the way past Mom to Corpse (Mother). Home (The Beast) comes via the Ascent.
  Players on that path need bombs and keys.

ROOMS AND WHAT OPENS THEM
- Treasure room: guaranteed on floors 1-6; free on floor 1, needs a KEY from floor 2. Alt-path
  treasure rooms hold 2+ items with one hidden. Skipping treasure rooms raises the chance of a
  Planetarium (one celestial item, same floors).
- Shop: every floor before the Womb; free on floor 1, needs a KEY after. Prices: item 15c (some
  actives 10c), soul heart / key / bomb / card / pill / battery 5c, red heart 3c, grab bag 7c.
  Sales halve a price; restocking shops raise prices after each buy.
- Secret and super secret rooms need a BOMB; the super secret one is usually near the boss room.
  Ultra secret: reached through red rooms (Red Key, Cracked Key, Soul of Cain).
- Key rooms: library (books, 1 key), dice room and vault (2 keys). Bedroom costs 2 bombs.
- Arcade (slots, beggars, fortune teller, blood donation): on even floors when the player ended
  the last floor with 5+ coins. Coins also go into beggars and slots, so they are never totally
  dead unless the floor has no shop, arcade, beggar or machine.
- Challenge room: only enterable at full health (hearts >= containers). Boss challenge (floors
  2, 4, 6...): only enterable at <= 1 heart. Healing or hurting the player changes which one
  they can open.
- Curse room: spiked door hurts on entry (flight avoids it). Sacrifice room: spikes pay out
  pickups or chests; the 9th and 11th sacrifice summon the angels with the Mega Satan key pieces.

DEVIL AND ANGEL DEALS (after the boss, floor 2 to the Womb)
- Base chance is tiny and climbs with no RED-heart damage taken (in the boss fight, across the
  floor). Soul, black, bone and eternal heart damage doesn't count. Soul hearts protect their
  deal chance; red-only health makes every hit cost it. Bombing a beggar or shopkeeper raises it.
- Deals cost 1-2 red heart containers (3 soul hearts with no red; ??? pays 1-2 soul hearts;
  Keeper pays 15c/30c; The Lost takes one free). Taking any deal locks Angel rooms out for the
  run; skipping the first Devil room makes the next offer an Angel room.
- Angel rooms give soul and eternal hearts and holy items; their statues drop the Mega Satan key
  pieces.

HEALTH
- Damage order: soul/black hearts go before red hearts. Black heart breaking hits every enemy in
  the room for 40. An eternal heart held to the next floor becomes a heart container (two on one
  floor become one at once). Bone hearts take hits like a container and can be refilled with red.
  Rotten hearts hold half a heart and spawn blue flies on every clear.
- A golden heart adds no health and bursts into 3-8 coins on the last hit. Health caps at 12 hearts
  total.

LUCK
- Room-clear drops count luck from 0 to 10; more luck means more drops and more chests. Many tear
  effects and some trinkets also scale with luck, so a +1 is a real stat up. Chest, beggar, machine
  and deal contents ignore luck.

ITEMS
- Snapshot items carry quality (q0-q4). q4 is run-defining (Brimstone, Sacred Heart, Godhead,
  Mom's Knife, Epic Fetus...), q3 strong, q2 solid, q0-q1 situational or junk. Players read
  pedestals by quality, so a q1 they have to take hurts more than an empty room.
- Weapon replacers (Brimstone, Mom's Knife, Epic Fetus, Dr. Fetus, Tech X, Ludovico) override or
  mangle most tear-modifier items, so tear items are dead pedestals for those builds. Dr. Fetus /
  Epic Fetus make bomb pickups irrelevant to combat. Soy/Almond Milk (fast tiny tears) undercut a
  big-damage build.
- Skeleton Key (99 keys), Pyro (99 bombs) and Dollar (99 coins) end that resource's scarcity:
  drops of it stop mattering, and so does starving it. Rib of Greed tilts drops toward coins;
  Daemon's Tail turns most heart drops into keys.
- Curse of the Blind hides every pedestal until it's picked up: perfect cover for a bad item.

CHARACTERS (snapshot player.character; Tainted versions report the base name, player.tainted says
which. Starting items listed are the unlocked loadout: check the snapshot for what they really hold)
- Isaac: 3 red, 1 bomb, D6 (rerolls pedestals, so a bad item can be rerolled).
- Magdalene: 4 red, Yum Heart (heals; red hearts matter less).
- Cain: 2 red, 1 key, Lucky Foot (+luck, better pills and machines).
- Judas: 1 red, 3 coins, Book of Belial. Glass cannon: every hit is huge.
- ??? (Blue Baby): soul hearts only, can't hold red. Red heart drops are worthless; health ups
  become soul hearts.
- Eve: 2 red, Whore of Babylon and Dead Bird (stronger at <= 1 red heart). Healing her turns
  that off.
- Samson: 3 red, Bloody Lust (damage up each hit taken in a room).
- Azazel: 3 black hearts, flight, short Brimstone. Range and tear items barely help.
- Lazarus: 3 red, revives once with 1 heart as Risen Lazarus.
- Eden: random health, items and stats every run.
- The Lost: no health at all, dies to any hit; flight, spectral tears, Holy Mantle, Eternal D6.
  Every heart pickup and health up is worthless; devil deals are free. Shields, extra lives and
  homing are gold.
- Lilith: 1 red + 2 black, can't shoot; Incubus fights for her. Familiar items are gold.
- Keeper: COINS ARE HEALTH. Max 3 coin hearts, each hit costs a whole one, a coin heals one.
  Hearts turn into blue flies. Coin floods are healing, so a coin drought starves their health.
- Apollyon: 2 red, Void (eats actives and pedestals for stats).
- The Forgotten: bone hearts, melee club; swaps to The Soul (soul/black hearts go there).
- Bethany: 3 red, Book of Virtues; soul hearts become charges (not health).
- Jacob & Esau: two bodies, shared coins/keys/bombs, separate items and health; either dying
  ends the run.
- Tainted signatures: Isaac (8-item cap, items cycle), Magdalene (draining extra health), Cain
  (Bag of Crafting: pedestals become pickups and pickups are crafted into items, so pickup
  variety IS their item pool), Judas (Dark Arts, black hearts), ??? (Hold; poop instead of
  bombs), Eve (Sumptorium, clots), Samson (berserk), Azazel (sneeze), Lazarus (Flip, two forms),
  Eden (rerolls on every hit), Lost (no Holy Mantle; many weak items rerolled away), Lilith (Gello),
  Keeper (everything costs coins, even treasure items; coins rule), Apollyon (Abyss), Forgotten
  (Soul carries the body), Bethany (Lemegeton; red hearts are blood charges), Jacob (chased by
  Dark Esau).

CURSES, PILLS, CARDS
- Curses: Darkness (dark rooms), Lost (no map; more rooms), Unknown (health hidden), Maze (doors
  misroute; can carry them past locked doors for free), Blind (pedestals show '?'). Vanilla rarely
  curses a floor, so a curse every floor is the DM signing its name.
- Pills: 13 effects per run, one per colour, unknown until the first swallow. Players test
  unknown colours when healthy and fear Health Down, Bad Trip, Amnesia, Paralysis and I'm Excited.
  Horse pills are double strength. PHD / Virgo / Lucky Foot soften bad pills.
- Cards the player knows by heart: 0 Fool (back to start), I Magician (homing), II High Priestess
  (Mom's foot), XIX Sun (heal + map), XVI Tower (bombs around them), XVIII Moon (to the ultra secret
  room), Joker (to the devil/angel room), Hermit (to the shop), Chaos (instant kills), Credit Card
  (free shop), Get out of Jail Free (opens any door)."""

GATE_KNOWLEDGE = (
    "Isaac context: from the Womb (floor 7) on every hit costs a full heart, so one heart is one hit from death. "
    "Keys open treasure rooms (from floor 2), shops (from floor 2), libraries; bombs open secret rooms and tinted "
    "rocks; ~15 coins buys a shop item. For Keeper coins ARE health; The Lost has no health; ??? can't hold red "
    "hearts; Bethany turns soul hearts into charges, not health. Owning Skeleton Key / Pyro / Dollar makes that resource irrelevant. "
    "Mom under 20:00 opens the Boss Rush and Mom's Heart under 30:00 opens Hush, so near those marks time matters.")
