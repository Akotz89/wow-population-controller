import azerothcore as ac

print("CPI223_GENERAL_ENGINE_LOADED_V2", flush=True)

def on_login(player):
    print("CPI223_GENERAL_ENGINE_LOGIN_V2", flush=True)

ac.Register("PLAYER_ON_LOGIN", on_login)
