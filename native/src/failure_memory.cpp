#include "pbguide/failure_memory.hpp"

#include <algorithm>
#include <cctype>
#include <limits>
#include <stdexcept>
#include <utility>

namespace pbguide
{
namespace
{
bool IsValidStableId(std::string const& stableId)
{
    std::size_t const separator = stableId.find(':');
    if (separator == std::string::npos || separator == 0 || separator + 1 >= stableId.size() ||
        !std::islower(static_cast<unsigned char>(stableId.front())))
        return false;
    return std::all_of(stableId.begin(), stableId.end(), [](char value) {
        unsigned char const character = static_cast<unsigned char>(value);
        return std::isalnum(character) || value == '_' || value == '-' || value == '.' || value == ':';
    });
}

bool IsValidClassification(std::string const& classification)
{
    static std::vector<std::string> const allowed = {
        "content_ineligible",
        "executor_error",
        "mechanic_unsupported",
        "no_progress",
        "party_not_ready",
        "path_unreachable",
        "resource_shortage",
        "target_missing",
        "timeout",
    };
    return std::binary_search(allowed.begin(), allowed.end(), classification);
}

bool IsValidScope(EvidenceScope scope)
{
    return scope == EvidenceScope::Character || scope == EvidenceScope::Cohort || scope == EvidenceScope::Global;
}

bool IsLowerHexDigest(std::string const& value)
{
    return value.size() == 64 && std::all_of(value.begin(), value.end(), [](char character) {
        unsigned char const byte = static_cast<unsigned char>(character);
        return std::isdigit(byte) || (character >= 'a' && character <= 'f');
    });
}

void AppendString(std::string& output, std::string const& value)
{
    output += std::to_string(value.size());
    output += ':';
    output += value;
    output += '|';
}

void AppendNumber(std::string& output, std::uint64_t value)
{
    output += std::to_string(value);
    output += '|';
}

bool SameKey(FailureEvidence const& left, FailureEvidence const& right)
{
    return CanonicalFailureKey(left) == CanonicalFailureKey(right);
}

bool Matches(BotProgressSnapshot const& snapshot, FailureEvidence const& failure)
{
    bool const scopeMatches = failure.scope == EvidenceScope::Global ||
        (failure.scope == EvidenceScope::Character && failure.characterId == snapshot.characterId &&
            failure.profileId == snapshot.profileId) ||
        (failure.scope == EvidenceScope::Cohort && failure.profileId == snapshot.profileId);
    return scopeMatches && failure.phase == snapshot.phase && failure.mapId == snapshot.mapId &&
        failure.zoneId == snapshot.zoneId && failure.groupSize == snapshot.groupSize &&
        failure.coreRevision == snapshot.coreRevision && failure.playerbotsRevision == snapshot.playerbotsRevision &&
        failure.contentRevision == snapshot.contentRevision && failure.contentGraphDigest == snapshot.contentGraphDigest &&
        failure.capabilityFingerprint == snapshot.capabilityFingerprint;
}

void ValidateEvidence(FailureEvidence const& failure)
{
    if (!IsValidStableId(failure.stableId) || !IsValidScope(failure.scope) || failure.failureCount == 0 ||
        failure.characterId == 0 || failure.phase == 0 ||
        failure.groupSize == 0 || failure.lastFailureSeconds < 0 ||
        !IsValidClassification(failure.classification) || failure.coreRevision.empty() ||
        failure.playerbotsRevision.empty() || failure.contentRevision.empty() ||
        !IsLowerHexDigest(failure.contentGraphDigest) || failure.capabilityFingerprint.empty() ||
        (failure.scope != EvidenceScope::Global && failure.profileId.empty()))
        throw std::invalid_argument("failure memory contains invalid evidence");
}
}

std::string CanonicalFailureKey(FailureEvidence const& evidence)
{
    std::string result = "cpi-207-failure-key-v1|";
    AppendString(result, evidence.stableId);
    AppendNumber(result, static_cast<std::uint8_t>(evidence.scope));
    AppendNumber(result, evidence.scope == EvidenceScope::Character ? evidence.characterId : 0);
    AppendNumber(result, evidence.phase);
    AppendNumber(result, evidence.mapId);
    AppendNumber(result, evidence.zoneId);
    AppendNumber(result, evidence.groupSize);
    AppendString(result, evidence.coreRevision);
    AppendString(result, evidence.playerbotsRevision);
    AppendString(result, evidence.contentRevision);
    AppendString(result, evidence.contentGraphDigest);
    AppendString(result, evidence.scope == EvidenceScope::Global ? std::string{} : evidence.profileId);
    AppendString(result, evidence.capabilityFingerprint);
    return result;
}

FailureMemory::FailureMemory(std::vector<FailureEvidence> loadedEvidence) : entries_(std::move(loadedEvidence))
{
    ValidateAndCanonicalize();
}

FailureEvidence FailureMemory::Record(
    BotProgressSnapshot const& snapshot,
    std::string stableId,
    std::string classification,
    EvidenceScope scope)
{
    if (snapshot.characterId == 0 || snapshot.phase == 0 || snapshot.groupSize == 0 || snapshot.nowSeconds < 0 ||
        snapshot.coreRevision.empty() || snapshot.playerbotsRevision.empty() || snapshot.contentRevision.empty() ||
        !IsLowerHexDigest(snapshot.contentGraphDigest) || snapshot.profileId.empty() ||
        snapshot.capabilityFingerprint.empty() || !IsValidStableId(stableId) ||
        !IsValidClassification(classification) || !IsValidScope(scope))
        throw std::invalid_argument("cannot record failure from invalid snapshot or observation");

    FailureEvidence observation;
    observation.stableId = std::move(stableId);
    observation.scope = scope;
    observation.characterId = snapshot.characterId;
    observation.phase = snapshot.phase;
    observation.mapId = snapshot.mapId;
    observation.zoneId = snapshot.zoneId;
    observation.groupSize = snapshot.groupSize;
    observation.failureCount = 1;
    observation.lastFailureSeconds = snapshot.nowSeconds;
    observation.classification = std::move(classification);
    observation.coreRevision = snapshot.coreRevision;
    observation.playerbotsRevision = snapshot.playerbotsRevision;
    observation.contentRevision = snapshot.contentRevision;
    observation.contentGraphDigest = snapshot.contentGraphDigest;
    observation.profileId = scope == EvidenceScope::Global ? std::string{} : snapshot.profileId;
    observation.capabilityFingerprint = snapshot.capabilityFingerprint;

    auto const found = std::find_if(entries_.begin(), entries_.end(), [&](FailureEvidence const& existing) {
        return SameKey(existing, observation);
    });
    if (found != entries_.end())
    {
        if (snapshot.nowSeconds < found->lastFailureSeconds)
            throw std::invalid_argument("failure observations must be monotonic within one context");
        if (found->failureCount == std::numeric_limits<std::uint32_t>::max())
            throw std::overflow_error("failure count overflow");
        ++found->failureCount;
        found->lastFailureSeconds = snapshot.nowSeconds;
        found->classification = observation.classification;
        return *found;
    }

    std::string const insertedKey = CanonicalFailureKey(observation);
    entries_.push_back(std::move(observation));
    std::sort(entries_.begin(), entries_.end(), [](FailureEvidence const& left, FailureEvidence const& right) {
        return CanonicalFailureKey(left) < CanonicalFailureKey(right);
    });
    auto const inserted = std::find_if(entries_.begin(), entries_.end(), [&](FailureEvidence const& existing) {
        return CanonicalFailureKey(existing) == insertedKey;
    });
    if (inserted == entries_.end())
        throw std::logic_error("recorded failure disappeared during canonicalization");
    return *inserted;
}

std::vector<FailureEvidence> FailureMemory::Applicable(BotProgressSnapshot const& snapshot) const
{
    std::vector<FailureEvidence> result;
    for (FailureEvidence const& failure : entries_)
    {
        if (Matches(snapshot, failure))
            result.push_back(failure);
    }
    return result;
}

std::vector<FailureEvidence> const& FailureMemory::Entries() const noexcept
{
    return entries_;
}

void FailureMemory::ValidateAndCanonicalize()
{
    for (FailureEvidence const& failure : entries_)
        ValidateEvidence(failure);
    std::sort(entries_.begin(), entries_.end(), [](FailureEvidence const& left, FailureEvidence const& right) {
        return CanonicalFailureKey(left) < CanonicalFailureKey(right);
    });
    for (std::size_t index = 1; index < entries_.size(); ++index)
    {
        if (SameKey(entries_[index - 1], entries_[index]))
            throw std::invalid_argument("failure memory contains a duplicate context key");
    }
}
}
