#pragma once

#include "pbguide/director.hpp"

#include <string>
#include <vector>

namespace pbguide
{
[[nodiscard]] std::string CanonicalFailureKey(FailureEvidence const& evidence);

class FailureMemory
{
public:
    FailureMemory() = default;
    explicit FailureMemory(std::vector<FailureEvidence> loadedEvidence);

    [[nodiscard]] FailureEvidence Record(
        BotProgressSnapshot const& snapshot,
        std::string stableId,
        std::string classification,
        EvidenceScope scope = EvidenceScope::Character);

    [[nodiscard]] std::vector<FailureEvidence> Applicable(BotProgressSnapshot const& snapshot) const;
    [[nodiscard]] std::vector<FailureEvidence> const& Entries() const noexcept;

private:
    void ValidateAndCanonicalize();

    std::vector<FailureEvidence> entries_;
};
}
