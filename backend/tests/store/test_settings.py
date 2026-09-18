import models
from store import settings


async def test_app_settings_are_saved_and_loaded():
    assert await settings.get() == models.AppSettings()

    configured = models.AppSettings(
        memory_repository="acme/hatchery-memory",
        memory_repository_installation_id="42",
    )

    assert await settings.save(configured) == configured
    assert await settings.get() == configured
