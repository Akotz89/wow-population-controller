"""General scripting entry point; gameplay consumers are added independently."""
import azerothcore as ac

ENGINE_READY = True
ENGINE_STARTUPS = 0


def on_startup():
    global ENGINE_STARTUPS
    ENGINE_STARTUPS += 1


ac.Register("WORLD_ON_STARTUP", on_startup)
