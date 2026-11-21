from flask import jsonify

_PALETTES = [
    ("0e0b14", "f6e7d8", "ff7a45", "ffd166"),
    ("071a1f", "e8fbf7", "21c7a8", "b8fff3"),
    ("fbf7f0", "1e1b18", "d94f30", "f2b705"),
    ("12001f", "f4e9ff", "c64bff", "ff9ef5"),
    ("1b2430", "edf1f7", "5c8dff", "8fe3cf"),
    ("f4e3d7", "2a211c", "b4552d", "e9a23b"),
    ("0f1d14", "eaf7ec", "4cd964", "d4f15a"),
    ("f5f5f5", "111111", "0057ff", "ffd400"),
    ("2b0f1a", "fff1f3", "ff5d73", "ffc857"),
    ("0a0f2c", "eaf0ff", "7c5cff", "5fe1d6"),
]

_PRESETS = [
    ("quick-launch", "Presets", "Quick Launch", "Start your story in 3 taps", "Make it yours", "Presets ready to edit", 0),
    ("editor-pick", "Recommended", "Editor's Pick", "This week's sharpest cut", "Try the recommended flow", "Built for fast edits", 1),
    ("fresh-frame", "New", "Fresh Frame", "Editable sample opening copy", "Be first to post", "Editable sample closing copy", 2),
    ("spotlight-ad", "Ads", "Spotlight Ad", "One offer. Zero noise.", "Book the spotlight", "Editable sample closing copy", 3),
    ("cart-drop", "E-commerce", "Cart Drop", "New arrivals are live", "Shop the drop", "Editable sample closing copy", 4),
    ("golden-season", "Seasonal", "Golden Season", "Warm light, short days", "Save the season", "Holiday edits inside", 5),
    ("lesson-one", "Education", "Lesson One", "Today we learn one thing", "Keep learning", "Editable sample closing copy", 6),
    ("quarter-brief", "Corporate", "Quarter Brief", "Q3 numbers, clearly", "Download the brief", "Boardroom ready", 7),
    ("story-beat", "Social", "Story Beat", "Post it before noon", "Share the beat", "Tag your crew", 8),
    ("open-house", "Real Estate", "Open House", "Sunday, 1 to 4", "Tour the home", "Book a private walk", 9),
    ("window-seat", "Travel", "Window Seat", "Three days, one bag", "Go somewhere", "Itinerary in captions", 0),
    ("spec-sheet", "Product", "Spec Sheet", "Built small. Works big.", "See the specs", "Editable sample closing copy", 1),
    ("save-the-date", "Events", "Save the Date", "Doors open at 7", "You're invited", "RSVP by Friday", 2),
    ("teaser-cut", "Trailers", "Teaser Cut", "In theaters Friday", "Watch the trailer", "Editable sample closing copy", 3),
    ("caption-chaos", "Memes", "Caption Chaos", "Nobody asked but here we are", "Send this to them", "No context needed", 4),
    ("creator-log", "Creator", "Creator Log", "Day 12 of the build", "Follow the log", "New video Thursday", 5),
    ("mic-check", "Podcast", "Mic Check", "Episode 88 is live", "Listen now", "Editable sample closing copy", 6),
    ("sound-drop", "Music", "Sound Drop", "Single out at midnight", "Play it loud", "Pre-save in bio", 7),
    ("respawn", "Gaming", "Respawn", "Patch notes are here", "Queue up", "Squad fills at 8", 8),
    ("final-whistle", "Sports", "Final Whistle", "Highlights in 60 seconds", "Catch the replay", "Editable sample closing copy", 9),
    ("morning-brief", "News", "Morning Brief", "What changed overnight", "Read the brief", "Editable sample closing copy", 0),
    ("field-notes", "Documentary", "Field Notes", "One river, twelve voices", "Watch the film", "Director's cut inside", 1),
    ("give-together", "Nonprofit", "Give Together", "Small gifts, big lift", "Join the cause", "Donate this week", 2),
    ("civic-note", "Government", "Civic Note", "Service update for residents", "Check the notice", "Editable sample closing copy", 3),
    ("reset-minute", "Health", "Reset Minute", "Two minutes to breathe", "Start the reset", "Editable sample closing copy", 4),
    ("table-for-two", "Food", "Table for Two", "Brown butter, no rush", "Get the recipe", "Save for Sunday", 5),
    ("fit-check", "Fashion", "Fit Check", "Layers for cold mornings", "Shop the fit", "Editable sample closing copy", 6),
    ("ship-log", "Tech", "Ship Log", "Version 4 is out", "Read the changelog", "Editable sample closing copy", 7),
    ("bottom-line", "Finance", "Bottom Line", "Rates moved this week", "See the numbers", "No jargon, promise", 8),
    ("road-test", "Auto", "Road Test", "600 miles, one tank", "Watch the test", "Specs in captions", 9),
    ("vow-reel", "Wedding", "Vow Reel", "June 14, golden hour", "Save the date", "RSVP by May 1", 0),
    ("tiny-explorer", "Kids", "Tiny Explorer", "Colors, bugs, and mud", "Watch and wonder", "Parent guide inside", 1),
    ("quiet-light", "Faith", "Quiet Light", "A moment of stillness", "Join us Sunday", "All are welcome", 2),
    ("lab-note", "Science", "Lab Note", "Three minutes on tides", "See the data", "Sources in captions", 3),
    ("gallery-wall", "Museum", "Gallery Wall", "New wing opens today", "Plan your visit", "Editable sample closing copy", 4),
]
_LAYOUTS = ("center", "left", "stripe")
_RATIOS = ["16:9", "9:16", "1:1"]


def register_templates(app, staff):
    @app.get("/api/library/timeline-templates")
    def timeline_templates():
        staff()

        out = []
        for i, (slug, category, name, open_sub, close_title, close_sub, pi) in enumerate(_PRESETS):
            bg, fg, accent, pop = _PALETTES[pi]
            layout = _LAYOUTS[i % 3]
            card = {
                "title": name,
                "subtitle": open_sub,
                "background": bg,
                "foreground": fg,
                "accent": accent,
                "layout": layout,
            }
            close_card = {
                "title": close_title,
                "subtitle": close_sub,
                "background": bg,
                "foreground": fg,
                "accent": pop,
                "layout": layout,
            }
            out.append({
                "id": slug,
                "title": name,
                "category": category,
                "description": f"{name}: {open_sub}.",
                "ratios": list(_RATIOS),
                "opening": {"card": card, "start": 0, "end": 4000},
                "closing": {"card": close_card, "start": 0, "end": 3000},
            })
        return jsonify(templates=out)
