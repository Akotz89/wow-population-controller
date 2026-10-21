#include "pbguide/activity_reservations.hpp"

#include <algorithm>
#include <cctype>
#include <stdexcept>
#include <unordered_set>

namespace pbguide
{
namespace
{
bool IsStableId(std::string const& value)
{
    if (value.empty() || value.size() > 160 || !std::islower(static_cast<unsigned char>(value.front())))
        return false;

    auto const separator = value.find(':');
    if (separator == std::string::npos || separator == 0 || separator + 1 == value.size())
        return false;

    auto const prefixValid = std::all_of(value.begin(), value.begin() + static_cast<std::ptrdiff_t>(separator),
        [](unsigned char character)
        {
            return std::islower(character) || std::isdigit(character) || character == '_' || character == '-';
        });
    auto const suffixValid = std::all_of(value.begin() + static_cast<std::ptrdiff_t>(separator + 1), value.end(),
        [](unsigned char character)
        {
            return std::isalnum(character) || character == '_' || character == '-' || character == '.' ||
                character == ':';
        });
    return prefixValid && suffixValid;
}

bool IsValidKind(ActivityKind kind)
{
    switch (kind)
    {
        case ActivityKind::SoloObjective:
        case ActivityKind::OutdoorQuestGroup:
        case ActivityKind::DungeonLfg:
        case ActivityKind::GuildObjective:
        case ActivityKind::WorldPvp:
            return true;
    }
    return false;
}

bool IsValidReservation(ActivityReservation const& reservation)
{
    return reservation.characterId != 0 && IsStableId(reservation.activityId) &&
        IsStableId(reservation.ownerId) && IsValidKind(reservation.kind) && reservation.expiresAtSeconds >= 0;
}
}

ActivityReservationBook::ActivityReservationBook(std::vector<ActivityReservation> persisted)
{
    std::sort(persisted.begin(), persisted.end(), [](auto const& left, auto const& right)
    {
        return left.characterId < right.characterId;
    });

    for (auto const& reservation : persisted)
    {
        if (!IsValidReservation(reservation))
            throw std::invalid_argument("invalid persisted activity reservation");
        if (!reservations_.emplace(reservation.characterId, reservation).second)
            throw std::invalid_argument("duplicate persisted character activity reservation");
    }
}

ActivityReservationDecision ActivityReservationBook::Acquire(
    ActivityReservationRequest const& request,
    std::int64_t nowSeconds)
{
    ActivityReservationDecision decision;
    static_cast<void>(Expire(nowSeconds));

    if (nowSeconds < 0 || request.expiresAtSeconds <= nowSeconds || !IsStableId(request.activityId) ||
        !IsStableId(request.ownerId) || !IsValidKind(request.kind) || request.characterIds.empty() ||
        request.characterIds.size() > 40)
    {
        decision.rejectionReason = "invalid_reservation_request";
        return decision;
    }

    std::unordered_set<std::uint64_t> requested;
    for (std::uint64_t characterId : request.characterIds)
    {
        if (characterId == 0 || !requested.insert(characterId).second)
        {
            decision.rejectionReason = "invalid_reservation_request";
            return decision;
        }

        auto const existing = reservations_.find(characterId);
        if (existing != reservations_.end() &&
            (existing->second.activityId != request.activityId || existing->second.ownerId != request.ownerId ||
                existing->second.kind != request.kind))
        {
            decision.conflictingCharacterIds.push_back(characterId);
        }
    }

    if (!decision.conflictingCharacterIds.empty())
    {
        std::sort(decision.conflictingCharacterIds.begin(), decision.conflictingCharacterIds.end());
        decision.rejectionReason = "character_activity_conflict";
        return decision;
    }

    for (std::uint64_t characterId : request.characterIds)
    {
        reservations_[characterId] = {
            characterId,
            request.activityId,
            request.ownerId,
            request.kind,
            request.expiresAtSeconds,
        };
    }

    decision.acquired = true;
    return decision;
}

std::size_t ActivityReservationBook::Release(std::string const& activityId, std::string const& ownerId)
{
    std::size_t released = 0;
    for (auto iterator = reservations_.begin(); iterator != reservations_.end();)
    {
        if (iterator->second.activityId == activityId && iterator->second.ownerId == ownerId)
        {
            iterator = reservations_.erase(iterator);
            ++released;
        }
        else
        {
            ++iterator;
        }
    }
    return released;
}

bool ActivityReservationBook::ReleaseCharacter(
    std::uint64_t characterId,
    std::string const& activityId,
    std::string const& ownerId)
{
    auto const reservation = reservations_.find(characterId);
    if (reservation == reservations_.end() || reservation->second.activityId != activityId ||
        reservation->second.ownerId != ownerId)
    {
        return false;
    }

    reservations_.erase(reservation);
    return true;
}

std::size_t ActivityReservationBook::Expire(std::int64_t nowSeconds)
{
    if (nowSeconds < 0)
        return 0;

    std::size_t expired = 0;
    for (auto iterator = reservations_.begin(); iterator != reservations_.end();)
    {
        if (iterator->second.expiresAtSeconds <= nowSeconds)
        {
            iterator = reservations_.erase(iterator);
            ++expired;
        }
        else
        {
            ++iterator;
        }
    }
    return expired;
}

std::optional<ActivityReservation> ActivityReservationBook::ActiveFor(
    std::uint64_t characterId,
    std::int64_t nowSeconds) const
{
    if (nowSeconds < 0)
        return std::nullopt;

    auto const reservation = reservations_.find(characterId);
    if (reservation == reservations_.end() || reservation->second.expiresAtSeconds <= nowSeconds)
        return std::nullopt;
    return reservation->second;
}

std::vector<ActivityReservation> ActivityReservationBook::Entries() const
{
    std::vector<ActivityReservation> entries;
    entries.reserve(reservations_.size());
    for (auto const& [characterId, reservation] : reservations_)
    {
        static_cast<void>(characterId);
        entries.push_back(reservation);
    }
    return entries;
}
}
