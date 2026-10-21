#pragma once
// Internal shared strict JSON contract. Lives in director; replay depends on director.
#include <boost/json.hpp>
#include <cstdint>
#include <cstddef>
#include <string>
#include <string_view>
namespace pbguide::json_contract {
inline constexpr std::size_t MaxDocumentBytes = 16 * 1024 * 1024;
boost::json::value Parse(std::string_view);
void Validate(boost::json::value const&, std::string_view schemaName);
std::string Canonical(boost::json::value const&);
std::string Digest(boost::json::value const&);
std::uint64_t Unsigned(boost::json::value const&);
std::int64_t Signed(boost::json::value const&);
}
