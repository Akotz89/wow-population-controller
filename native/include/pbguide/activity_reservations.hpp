#pragma once

#include <cstddef>
#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <vector>

namespace pbguide
{
enum class ActivityKind : std::uint8_t
{
    SoloObjective,
    OutdoorQuestGroup,
    DungeonLfg,
    GuildObjective,
    WorldPvp
};

struct ActivityReservation
{
    std::uint64_t characterId = 0;
    std::string activityId;
    std::string ownerId;
    ActivityKind kind = ActivityKind::SoloObjective;
    std::int64_t expiresAtSeconds = 0;
};

struct ActivityReservationRequest
{
    std::string activityId;
    std::string ownerId;
    ActivityKind kind = ActivityKind::SoloObjective;
    std::vector<std::uint64_t> characterIds;
    std::int64_t expiresAtSeconds = 0;
};

struct ActivityReservationDecision
{
    bool acquired = false;
    std::string rejectionReason;
    std::vector<std::uint64_t> conflictingCharacterIds;
};

class ActivityReservationBook
{
public:
    explicit ActivityReservationBook(std::vector<ActivityReservation> persisted = {});

    [[nodiscard]] ActivityReservationDecision Acquire(
        ActivityReservationRequest const& request,
        std::int64_t nowSeconds);
    [[nodiscard]] std::size_t Release(std::string const& activityId, std::string const& ownerId);
    [[nodiscard]] bool ReleaseCharacter(
        std::uint64_t characterId,
        std::string const& activityId,
        std::string const& ownerId);
    [[nodiscard]] std::size_t Expire(std::int64_t nowSeconds);
    [[nodiscard]] std::optional<ActivityReservation> ActiveFor(
        std::uint64_t characterId,
        std::int64_t nowSeconds) const;
    [[nodiscard]] std::vector<ActivityReservation> Entries() const;

private:
    std::map<std::uint64_t, ActivityReservation> reservations_;
};
}
