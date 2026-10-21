#include "pbguide/canonical_item_sources.hpp"
#include "canonical_schemas.hpp"
#include "json_contract.hpp"
#include <boost/json.hpp>
#include <openssl/sha.h>
#include <algorithm>
#include <array>
#include <chrono>
#include <functional>
#include <iomanip>
#include <limits>
#include <regex>
#include <sstream>
#include <stdexcept>
#include <tuple>
namespace pbguide {
struct SourcePredicateProgram {
 enum class State { Unknown, False, True };
 enum class Op { True, False, Unknown, Leaf, Reference, Not, All, Any };
 struct Expression {
  Op op = Op::Unknown;
  std::vector<std::size_t> arguments;
  std::string reference, definitionDigest;
  std::uint32_t conditionType = 0;
  std::array<std::uint32_t,3> values{};
 };
 struct Availability {
  std::string locus, locusKind, ownerKind, conditionId, astPrefix = "$";
  std::uint32_t ownerId = 0;
  std::uint32_t minimumFrontier = 0, maximumFrontier = 0, phaseMask = 0, spawnMask = 0;
  std::vector<std::uint32_t> eventIds, poolIds;
  std::optional<std::uint32_t> reagentItemId;
  bool projectedAvailable = true;
 };
 std::vector<Expression> expressions;
 std::map<std::string,std::size_t> roots;
 std::map<std::string,std::vector<Availability>> requirements;
 struct Source { std::string kind; std::uint32_t id=0,itemId=0; boost::json::value reward; };
 struct Query { std::size_t expression=0,requirement=0;boost::json::value row; };
 std::map<std::string,Source> sources;
 std::map<std::string,std::vector<Query>> queries;
};
namespace {
namespace j=boost::json;
using V=j::value; using O=j::object; using A=j::array;
[[noreturn]] void fail(std::string const& reason) { throw std::invalid_argument("canonical_item_sources:"+reason); }
using PredicateState = SourcePredicateProgram::State;
class PredicateEvaluation {
 SourcePredicateProgram const& program;
 std::map<std::size_t,PredicateState> values;
 std::set<std::size_t> active;
 std::function<PredicateState(std::size_t)> leaf;
 std::size_t* work;
 PredicateState expression(std::size_t index,unsigned depth) {
  if(depth>128)fail("condition_depth");
  if(auto found=values.find(index);found!=values.end())return found->second;
  if(work&&++*work>16384)fail("predicate_work_bound");
  if(!active.insert(index).second)fail("condition_cycle");
  auto const& e=program.expressions.at(index);
  using Op=SourcePredicateProgram::Op;
  PredicateState result=PredicateState::Unknown;
  if(e.op==Op::True)result=PredicateState::True;
  else if(e.op==Op::False)result=PredicateState::False;
  else if(e.op==Op::Leaf)result=leaf(index);
  else if(e.op==Op::Reference)result=condition(e.reference,depth+1);
  else if(e.op==Op::Not){auto value=expression(e.arguments.at(0),depth+1);result=value==PredicateState::Unknown?value:value==PredicateState::True?PredicateState::False:PredicateState::True;}
  else if(e.op==Op::All||e.op==Op::Any){
   bool unknown=false,yes=false,no=false;
   // Visit every branch so a decisive sibling cannot hide a missing reference or cycle.
   for(auto argument:e.arguments){auto value=expression(argument,depth+1);unknown|=value==PredicateState::Unknown;yes|=value==PredicateState::True;no|=value==PredicateState::False;}
   result=e.op==Op::All?(no?PredicateState::False:unknown?PredicateState::Unknown:PredicateState::True):(yes?PredicateState::True:unknown?PredicateState::Unknown:PredicateState::False);
  }
  active.erase(index);values.emplace(index,result);return result;
 }
public:
 explicit PredicateEvaluation(SourcePredicateProgram const& source,
     std::function<PredicateState(std::size_t)> resolve=[](std::size_t){return PredicateState::Unknown;},std::size_t* budget=nullptr)
     : program(source),leaf(std::move(resolve)),work(budget) {}
 PredicateState condition(std::string const& id,unsigned depth=0){auto found=program.roots.find(id);if(found==program.roots.end())fail("missing_condition:"+id);return expression(found->second,depth);}
};
std::string str(V const& v) { return std::string(v.as_string()); }
std::uint64_t num(V const& v) { if(v.is_uint64()) return v.as_uint64(); if(v.is_int64()&&v.as_int64()>=0) return static_cast<std::uint64_t>(v.as_int64()); fail("unsigned_integer"); }
// Preliminary recursive token walk rejects duplicate keys (including escaped spellings)
// before Boost's DOM parser could discard them. DOM parsing supplies full JSON/UTF8 grammar validation.
class UniqueKeys {
 std::string_view s; std::size_t p=0;
 void ws(){while(p<s.size()&&(s[p]==' '||s[p]=='\n'||s[p]=='\r'||s[p]=='\t'))++p;}
 std::string token(){std::size_t b=p++; bool esc=false;while(p<s.size()){char c=s[p++];if(!esc&&c=='"')return str(j::parse(s.substr(b,p-b)));if(!esc&&c=='\\')esc=true;else esc=false;}fail("unterminated_string");}
 void value(unsigned depth){if(depth>128)fail("json_depth");ws();if(p>=s.size())fail("json_truncated");char c=s[p++];if(c=='"'){--p;token();return;}if(c=='{'||c=='['){std::set<std::string> keys;ws();char end=c=='{'?'}':']';if(p<s.size()&&s[p]==end){++p;return;}while(true){ws();if(c=='{'){if(p>=s.size()||s[p]!='"')fail("object_key");if(!keys.insert(token()).second)fail("duplicate_json_key");ws();if(p>=s.size()||s[p++]!=':')fail("object_colon");}value(depth+1);ws();if(p>=s.size())fail("json_truncated");char next=s[p++];if(next==end)return;if(next!=',')fail("json_separator");}}while(p<s.size()&&s[p]!=','&&s[p]!=']'&&s[p]!='}'&&s[p]!=' '&&s[p]!='\n'&&s[p]!='\r'&&s[p]!='\t')++p;}
 public: explicit UniqueKeys(std::string_view bytes):s(bytes){} void check(){value(0);ws();if(p!=s.size())fail("trailing_json");}
};
V parse(std::string_view s){if(s.size()>json_contract::MaxDocumentBytes)fail("byte_bound");UniqueKeys(s).check();return j::parse(s);}
std::string canonical(V const& v){
 if(v.is_object()){std::map<std::string,V const*> sorted;for(auto const& kv:v.as_object())sorted.emplace(std::string(kv.key()),&kv.value());std::string r="{";for(auto const& [k,x]:sorted){if(r.size()>1)r+=",";r+=j::serialize(V(k))+":"+canonical(*x);}return r+"}";}
 if(v.is_array()){std::string r="[";for(auto const& x:v.as_array()){if(r.size()>1)r+=",";r+=canonical(x);}return r+"]";}
 if(v.is_double())fail("floating_point_attributes_unsupported");
 return j::serialize(v);
}
std::string digest(V const& v){auto bytes=canonical(v);unsigned char hash[SHA256_DIGEST_LENGTH];SHA256(reinterpret_cast<unsigned char const*>(bytes.data()),bytes.size(),hash);std::ostringstream out;for(auto b:hash)out<<std::hex<<std::setw(2)<<std::setfill('0')<<static_cast<unsigned>(b);return out.str();}
void sortBy(A& a,char const* key){std::sort(a.begin(),a.end(),[&](V const& l,V const& r){return str(l.at(key))<str(r.at(key));});}
void sortSet(A& a){std::sort(a.begin(),a.end(),[](V const& l,V const& r){return canonical(l)<canonical(r);});}
void calendarDateTime(std::string const& text){
 int y=std::stoi(text.substr(0,4)),m=std::stoi(text.substr(5,2)),d=std::stoi(text.substr(8,2));
 std::chrono::year_month_day date{std::chrono::year{y},std::chrono::month{static_cast<unsigned>(m)},std::chrono::day{static_cast<unsigned>(d)}};
 if(std::stoi(text.substr(17,2))==60)fail("datetime_leap_second_unsupported");
 if(y<1||!date.ok()||std::stoi(text.substr(11,2))>23||std::stoi(text.substr(14,2))>59||std::stoi(text.substr(17,2))>59)fail("schema_datetime_calendar");
 if(text.back()!='Z'&&text.back()!='z'){auto offset=text.size()-6;if(std::stoi(text.substr(offset+1,2))>23||std::stoi(text.substr(offset+4,2))>59)fail("schema_datetime_offset");}
}
// JSON parsing has already validated UTF-8; each noncontinuation byte begins one codepoint.
std::size_t codepoints(std::string const& text){return static_cast<std::size_t>(std::count_if(text.begin(),text.end(),[](char byte){return (static_cast<unsigned char>(byte)&0xc0u)!=0x80u;}));}
// Validate the exact shipped schemas, rather than maintaining weaker duplicate field contracts.
void schema(V const& v,V const& s,V const& root,V const& docs,unsigned depth=0){
 if(depth>256)fail("schema_depth");
 auto const& o=s.as_object();
 if(auto r=o.if_contains("$ref")){auto ref=str(*r);auto hash=ref.find('#');std::string file=ref.substr(0,hash);V const* target=file.empty()?&root:&docs.at(file);std::string path=ref.substr(hash+1);std::size_t start=1;while(start<path.size()){auto end=path.find('/',start);target=&target->at(path.substr(start,end-start));if(end==std::string::npos)break;start=end+1;}schema(v,*target,file.empty()?root:docs.at(file),docs,depth+1);return;}
 for(auto key:{"oneOf","anyOf"})if(auto alternatives=o.if_contains(key)){unsigned valid=0;for(auto const& alt:alternatives->as_array()){try{schema(v,alt,root,docs,depth+1);++valid;}catch(std::invalid_argument const&){}}if(valid==0||(std::string(key)=="oneOf"&&valid!=1))fail("schema_alternative");}
 if(auto c=o.if_contains("const");c&&v!=*c)fail("schema_const");
 if(auto e=o.if_contains("enum");e&&std::find(e->as_array().begin(),e->as_array().end(),v)==e->as_array().end())fail("schema_enum");
 auto type=[&](std::string const& t){return t=="object"?v.is_object():t=="array"?v.is_array():t=="string"?v.is_string():t=="integer"?(v.is_int64()||v.is_uint64()):t=="boolean"?v.is_bool():t=="null"?v.is_null():false;};
 if(auto t=o.if_contains("type")){bool ok=false;if(t->is_array()){for(auto const& x:t->as_array())ok|=type(str(x));}else ok=type(str(*t));if(!ok)fail("schema_type");}
 if(v.is_int64()||v.is_uint64()){
  auto less=[](V const& a,V const& b){
   bool an=a.is_int64()&&a.as_int64()<0,bn=b.is_int64()&&b.as_int64()<0;
   if(an!=bn)return an;
   if(an)return a.as_int64()<b.as_int64();
   return num(a)<num(b);
  };
  for(auto key:{"minimum","maximum"})if(auto b=o.if_contains(key))
   if((std::string(key)=="minimum"&&less(v,*b))||(std::string(key)=="maximum"&&less(*b,v)))fail("schema_numeric_bound");
 }
 if(v.is_string()){auto text=str(v);if(auto b=o.if_contains("maxLength");b&&codepoints(text)>num(*b))fail("schema_string_bound");if(auto b=o.if_contains("minLength");b&&codepoints(text)<num(*b))fail("schema_string_bound");if(auto pat=o.if_contains("pattern");pat&&!std::regex_search(text,std::regex(str(*pat))))fail("schema_pattern");if(auto fmt=o.if_contains("format");fmt&&str(*fmt)=="date-time"&&!std::regex_match(text,std::regex("[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]+)?([Zz]|[+-][0-9]{2}:[0-9]{2})")))fail("schema_datetime");if(auto fmt=o.if_contains("format");fmt&&str(*fmt)=="date-time")calendarDateTime(text);}
 if(v.is_array()){auto const& a=v.as_array();if(auto b=o.if_contains("maxItems");b&&a.size()>num(*b))fail("schema_array_bound");if(auto b=o.if_contains("minItems");b&&a.size()<num(*b))fail("schema_array_bound");std::set<std::string> seen;for(auto const& x:a){if(auto unique=o.if_contains("uniqueItems");unique&&unique->as_bool()&&!seen.insert(canonical(x)).second)fail("schema_duplicate");if(auto items=o.if_contains("items"))schema(x,*items,root,docs,depth+1);}}
 if(v.is_object()){auto const& obj=v.as_object();if(auto req=o.if_contains("required"))for(auto const& key:req->as_array())if(!obj.contains(str(key)))fail("schema_required:"+str(key));for(auto const& kv:obj){if(auto pn=o.if_contains("propertyNames"))schema(V(kv.key()),*pn,root,docs,depth+1);V const* prop=nullptr;if(auto props=o.if_contains("properties"))prop=props->as_object().if_contains(kv.key());if(prop)schema(kv.value(),*prop,root,docs,depth+1);else if(auto ap=o.if_contains("additionalProperties")){if(ap->is_bool()){if(!ap->as_bool())fail("schema_extra:"+std::string(kv.key()));}else schema(kv.value(),*ap,root,docs,depth+1);}}}
}
std::map<std::string,std::string> const kinds={{"quest","quests"},{"item","items"},{"creature","creatures"},{"gameobject","gameobjects"},{"spell","spells"},{"trainer","trainers"},{"vendor","vendors"},{"taxi_node","taxi_nodes"},{"area","areas"},{"map","maps"},{"dungeon","dungeons"},{"loot_template","loot_templates"},{"recipe","recipes"}};
std::string identity(V const& ref){return str(ref.at("kind"))+":"+std::to_string(num(ref.at("id")));}
void normalizeExpression(V& e){auto op=str(e.at("op"));if(op=="all"||op=="any"){auto& a=e.at("arguments").as_array();for(auto& x:a)normalizeExpression(x);sortSet(a);}else if(op=="not")normalizeExpression(e.at("argument"));}
constexpr std::size_t MaxPredicateWork=16384;
V sourceBinding(OfflineSourcePins const& pins){return O{{"candidate_id",pins.candidateId},{"generation",pins.generation},{"graph_digest",pins.graphDigest},{"manifest_digest",pins.manifestDigest},{"projection_digest",pins.projectionDigest}};}
V leafDefinition(SourcePredicateProgram::Expression const& e){return O{{"op","leaf"},{"condition_type",e.conditionType},{"values",A{e.values[0],e.values[1],e.values[2]}}};}
void retainQueries(SourcePredicateProgram& program,OfflineSourcePins const& pins){
 using Op=SourcePredicateProgram::Op;
 for(auto const& [sourceId,requirements]:program.requirements){
  auto const& source=program.sources.at(sourceId);auto& queries=program.queries[sourceId];
  for(std::size_t requirementIndex=0;requirementIndex<requirements.size();++requirementIndex){
   auto const& requirement=requirements[requirementIndex];if(requirement.conditionId.empty())continue;
   std::set<std::size_t> visited;
   std::function<void(std::size_t,std::string,unsigned)> visit;
   visit=[&](std::size_t index,std::string path,unsigned depth){
    if(depth>128)fail("condition_depth");
    // Shared definitions use the first normalized traversal path as their query identity.
    if(!visited.insert(index).second)return;
    auto const& e=program.expressions.at(index);
    if(e.op==Op::Leaf){
     if(queries.size()>=MaxPredicateWork)fail("predicate_work_bound");
     V definition=leafDefinition(e);
     O identity{{"source_binding",sourceBinding(pins)},{"source_id",sourceId},{"source_ref",O{{"kind",source.kind},{"id",source.id}}},{"item_id",source.itemId},{"reward",source.reward},
       {"locus",O{{"kind",requirement.locusKind},{"owner_ref",O{{"kind",requirement.ownerKind},{"id",requirement.ownerId}}}}},{"condition_id",requirement.conditionId},{"ast_path",path},{"definition",definition}};
     auto predicateId=digest(identity);identity.erase("source_binding");identity.emplace("predicate_id",predicateId);identity.emplace("definition_sha256",e.definitionDigest);
     queries.push_back({index,requirementIndex,std::move(identity)});
    }else if(e.op==Op::Reference)visit(program.roots.at(e.reference),path+".reference("+e.reference+")",depth+1);
    else for(std::size_t i=0;i<e.arguments.size();++i)visit(e.arguments[i],path+(e.op==Op::Not?".argument":".arguments["+std::to_string(i)+"]"),depth+1);
   };
   visit(program.roots.at(requirement.conditionId),requirement.astPrefix,0);
  }
 }
}
std::set<std::string> sourceClosure(ItemSourceIndex const& index,std::vector<ItemUpgradeTarget> const& targets){
 std::set<std::uint32_t> items;std::set<std::string> sources;std::vector<std::uint32_t> pending;
 for(auto const& target:targets)pending.push_back(target.itemId);
 while(!pending.empty()){
  auto item=pending.back();pending.pop_back();if(!items.insert(item).second)continue;
  if(items.size()>MaxPredicateWork)fail("predicate_work_bound");
  for(auto const* source:index.SourcesForItem(item)){
   if(!sources.insert(source->stableId).second)continue;
   if(sources.size()>MaxPredicateWork)fail("predicate_work_bound");
   for(auto const& reagent:source->craftDependencies)pending.push_back(reagent.itemId);
  }
 }
 return sources;
}
V queryEnvelope(SourcePredicateProgram const& program,OfflineSourcePins const& pins,std::set<std::string> const& sources){
 std::map<std::string,V const*> rows;
 for(auto const& source:sources)for(auto const& query:program.queries.at(source)){
  auto id=str(query.row.at("predicate_id"));if(!rows.emplace(id,&query.row).second)fail("duplicate_predicate");
  if(rows.size()>MaxPredicateWork)fail("predicate_work_bound");
 }
 A predicates,ids;for(auto const& [id,row]:rows){ids.emplace_back(id);predicates.push_back(*row);}
 return O{{"schema","cpi211-source-predicate-query-v1"},{"source_binding",sourceBinding(pins)},{"predicates",std::move(predicates)},{"query_digest",digest(ids)}};
}
void exactKeys(V const& value,std::initializer_list<char const*> keys){
 auto const& object=value.as_object();if(object.size()!=keys.size())fail("actor_context_fields");
 for(auto key:keys)if(!object.contains(key))fail("actor_context_fields");
}
bool hexDigest(V const& value,bool allowUpper=false){if(!value.is_string())return false;auto text=value.as_string();return text.size()==64&&std::all_of(text.begin(),text.end(),[allowUpper](char c){return (c>='0'&&c<='9')||(c>='a'&&c<='f')||(allowUpper&&c>='A'&&c<='F');});}
void contextString(V const& value,bool nonempty=true){if(!value.is_string()||value.as_string().size()>4096||(nonempty&&value.as_string().empty()))fail("actor_context_string");}
std::map<std::string,PredicateState> contextVerdicts(std::string_view bytes,V const& envelope,BotProgressSnapshot const& snapshot){
 if(bytes.size()>4*1024*1024)fail("actor_context_byte_bound");
 auto context=parse(bytes);exactKeys(context,{"schema","expected","observed"});
 if(str(context.at("schema"))!="cpi211-actor-plan-context-v1")fail("actor_context_schema");
 auto const& expected=context.at("expected");exactKeys(expected,{"source_binding","actor","phase","evaluator_id","observed_state_generation"});
 if(expected.at("source_binding")!=envelope.at("source_binding"))fail("actor_context_source_binding");
 auto const& actor=expected.at("actor");exactKeys(actor,{"identity_key","account_id","character_guid","session_incarnation","strategic_generation"});
 contextString(actor.at("identity_key"));auto account=num(actor.at("account_id"));
 if(!account||account>std::numeric_limits<std::uint32_t>::max()||!num(actor.at("session_incarnation"))||!num(actor.at("character_guid"))||num(actor.at("character_guid"))!=snapshot.characterId)fail("actor_context_actor_binding");
 (void)num(actor.at("strategic_generation"));
 auto const& phase=expected.at("phase");exactKeys(phase,{"phase_id","policy_digest","planner_phase"});contextString(phase.at("phase_id"));
 if(!hexDigest(phase.at("policy_digest"),true)||num(phase.at("planner_phase"))!=snapshot.phase||snapshot.contentGraphDigest!=str(envelope.at("source_binding").at("graph_digest")))fail("actor_context_phase_binding");
 if(str(expected.at("evaluator_id"))!="core1-source-predicates-v1"||!hexDigest(expected.at("observed_state_generation")))fail("actor_context_evaluator_or_generation");
 auto const& observed=context.at("observed");exactKeys(observed,{"schema","valid","reason","binding","observed_state_generation","verdicts","query_digest"});
 if(str(observed.at("schema"))!="cpi211-source-predicate-context-v1"||!observed.at("valid").as_bool())fail("actor_context_invalid");
 contextString(observed.at("reason"),false);
 auto const& binding=observed.at("binding");exactKeys(binding,{"source_binding","actor","phase","evaluator_id"});
 for(auto key:{"source_binding","actor","phase","evaluator_id"})if(binding.at(key)!=expected.at(key))fail("actor_context_binding_mismatch");
 if(observed.at("observed_state_generation")!=expected.at("observed_state_generation"))fail("actor_context_stale_generation");
 if(observed.at("query_digest")!=envelope.at("query_digest"))fail("actor_context_query_digest");
 std::map<std::string,V const*> queries;for(auto const& row:envelope.at("predicates").as_array())queries.emplace(str(row.at("predicate_id")),&row);
 std::map<std::string,PredicateState> states;auto const& verdicts=observed.at("verdicts").as_array();if(verdicts.size()>MaxPredicateWork)fail("predicate_work_bound");
 for(auto const& verdict:verdicts){
  auto const& fields=verdict.as_object();bool matched=fields.contains("matched_definition")||fields.contains("matched_source");
  if(matched)exactKeys(verdict,{"predicate_id","definition_sha256","verdict","reason","matched_definition","matched_source"});
  else exactKeys(verdict,{"predicate_id","definition_sha256","verdict","reason"});
  auto id=str(verdict.at("predicate_id"));auto found=queries.find(id);if(found==queries.end())fail("actor_context_unknown_predicate");
  auto const& query=*found->second;if(verdict.at("definition_sha256")!=query.at("definition_sha256"))fail("actor_context_definition_digest");
  auto value=str(verdict.at("verdict"));if(value!="true"&&value!="false"&&value!="unknown")fail("actor_context_verdict");
  contextString(verdict.at("reason"),false);
  if(value!="unknown"&&!matched)fail("actor_context_missing_native_match");
  if(matched){
   V source=O{{"source_ref",query.at("source_ref")},{"item_id",query.at("item_id")},{"reward",query.at("reward")},{"locus_owner_ref",query.at("locus").at("owner_ref")}};
   if(verdict.at("matched_definition")!=query.at("definition")||verdict.at("matched_source")!=source)fail("actor_context_native_match_mismatch");
  }
  auto state=value=="true"?PredicateState::True:value=="false"?PredicateState::False:PredicateState::Unknown;
  if(!states.emplace(id,state).second)fail("actor_context_duplicate_predicate");
 }
 return states;
}
std::string availabilityReason(SourcePredicateProgram::Availability const& requirement,PredicateEvaluation& evaluation){
 if(requirement.phaseMask!=1||requirement.spawnMask!=1)return "mask_context_unavailable";
 if(!requirement.eventIds.empty())return "event_context_unavailable";
 if(!requirement.poolIds.empty())return "pool_context_unavailable";
 if(requirement.conditionId.empty())return {};
 auto value=evaluation.condition(requirement.conditionId);
 return value==PredicateState::False?"condition_false":value==PredicateState::Unknown?"condition_unknown":"";
}
std::pair<std::vector<ItemSourceOption>,std::map<std::string,std::string>> evaluateSources(
 SourcePredicateProgram const& program,ItemSourceIndex const& index,std::map<std::string,std::string> blocked,
 std::set<std::string> const& closure,std::map<std::string,PredicateState> const& states){
 auto options=index.Sources();std::size_t work=0;
 for(auto& option:options){
  if(!closure.contains(option.stableId))continue;
  blocked.erase(option.stableId);option.available=true;
  auto const& requirements=program.requirements.at(option.stableId);auto const& queries=program.queries.at(option.stableId);
  std::vector<std::map<std::size_t,PredicateState>> requirementLeaves(requirements.size());
  for(auto const& query:queries){
   if(++work>MaxPredicateWork)fail("predicate_work_bound");
   auto found=states.find(str(query.row.at("predicate_id")));auto state=found==states.end()?PredicateState::Unknown:found->second;
   auto [previous,inserted]=requirementLeaves.at(query.requirement).emplace(query.expression,state);
   if(!inserted){if(previous->second!=PredicateState::Unknown&&state!=PredicateState::Unknown&&previous->second!=state)fail("actor_context_inconsistent_verdict");if(state==PredicateState::Unknown)previous->second=state;}
  }
  std::set<std::uint32_t> resetDependencies;
  for(std::size_t at=0;at<requirements.size();++at){
   if(++work>MaxPredicateWork)fail("predicate_work_bound");
   auto const& requirement=requirements[at];
   auto const& leaves=requirementLeaves[at];
   PredicateEvaluation evaluation(program,[&](std::size_t expression){auto found=leaves.find(expression);return found==leaves.end()?PredicateState::Unknown:found->second;},&work);
   auto reason=availabilityReason(requirement,evaluation);
   if(requirement.reagentItemId){
    auto dependency=std::find_if(option.craftDependencies.begin(),option.craftDependencies.end(),[&](auto const& dep){return dep.itemId==*requirement.reagentItemId;});
    if(dependency==option.craftDependencies.end())fail("missing_retained_reagent");
    if(resetDependencies.insert(dependency->itemId).second)dependency->available=requirement.projectedAvailable;
    if(!reason.empty()){dependency->available=false;blocked.emplace(option.stableId,"craft_dependency:"+reason);}
   }else if(!reason.empty()){option.available=false;blocked.emplace(option.stableId,reason);}
  }
  if(!option.phaseAuthorized)blocked.emplace(option.stableId,"craft_dependency:empty_phase_intersection");
 }
 return {std::move(options),std::move(blocked)};
}
struct Adapter{ItemSourceKind kind;char const* node;char const* edge;char const* semantics;};
std::map<std::string,Adapter> const adapters={
 {"quest_reward",{ItemSourceKind::QuestReward,"quest","quest_reward","quest_reward"}},
 {"creature_loot",{ItemSourceKind::CreatureDrop,"creature","loot_source","creature_loot"}},
 {"gameobject_loot",{ItemSourceKind::GameObjectDrop,"gameobject","loot_source","gameobject_loot"}},
 {"vendor",{ItemSourceKind::Vendor,"vendor","vendor_source","vendor"}},
 {"trainer_recipe",{ItemSourceKind::TrainerRecipe,"trainer","trainer_spell","trainer_recipe"}},
 {"craft",{ItemSourceKind::Craft,"recipe","recipe_product","craft"}},
 {"pvp_reward",{ItemSourceKind::PvpReward,"vendor","vendor_source","pvp_reward"}},
 {"dungeon_quest",{ItemSourceKind::DungeonQuest,"quest","quest_reward","dungeon_quest"}},
 {"dungeon_drop",{ItemSourceKind::DungeonDrop,"creature","loot_source","dungeon_drop"}},
 {"disenchant",{ItemSourceKind::Disenchant,"item","loot_source","disenchant"}},
 {"prospecting",{ItemSourceKind::Prospecting,"item","loot_source","prospecting"}},
 {"milling",{ItemSourceKind::Milling,"item","loot_source","milling"}},
 {"fishing",{ItemSourceKind::Fishing,"loot_template","loot_source","fishing"}},
 {"skinning",{ItemSourceKind::Skinning,"creature","loot_source","skinning"}}};
}
namespace json_contract {
boost::json::value Parse(std::string_view bytes) {
 try { return parse(bytes); } catch (std::exception const&) { throw std::invalid_argument("json_contract:invalid_json"); }
}
void Validate(V const& value, std::string_view name) {
 try {
  static V const docs=j::parse(CanonicalSchemas);
  auto const& root=docs.at(std::string(name));schema(value,root,root,docs);
 } catch(std::exception const&) { throw std::invalid_argument("json_contract:invalid_schema"); }
}
std::string Canonical(V const& value) { return canonical(value); }
std::string Digest(V const& value) { return digest(value); }
std::uint64_t Unsigned(V const& value) { return num(value); }
std::int64_t Signed(V const& value) {
 if(value.is_int64())return value.as_int64();
 if(value.is_uint64()&&value.as_uint64()<=static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()))return static_cast<std::int64_t>(value.as_uint64());
 throw std::invalid_argument("json_contract:signed_integer");
}
}
EffectiveDatasetBinding EffectiveDatasetBinding::FromRootAcceptedManifest(std::string_view bytes, std::string_view expectedSha256) {
 auto hex=[](std::string_view x){return x.size()==64 && std::all_of(x.begin(),x.end(),[](char c){return (c>='0'&&c<='9')||(c>='a'&&c<='f');});};
 if(!hex(expectedSha256) || bytes.size()>1024*1024)fail("accepted_manifest_bound_or_hash");
 unsigned char hash[SHA256_DIGEST_LENGTH];SHA256(reinterpret_cast<unsigned char const*>(bytes.data()),bytes.size(),hash);
 std::ostringstream actual;for(auto b:hash)actual<<std::hex<<std::setw(2)<<std::setfill('0')<<static_cast<unsigned>(b);
 if(actual.str()!=expectedSha256)fail("accepted_manifest_hash_mismatch");
 auto accepted=parse(bytes);auto const& o=accepted.as_object();
 if(o.size()!=7 || str(o.at("schema_version"))!="cpi211-root-accepted-item-sources-v1" ||
    str(o.at("status"))!="ROOT_ACCEPTED_EFFECTIVE_REALM_ITEM_SOURCES")fail("accepted_manifest_status_or_schema");
 OfflineSourcePins pins{str(o.at("candidate_id")),str(o.at("generation")),str(o.at("graph_digest")),str(o.at("manifest_digest")),str(o.at("projection_digest"))};
 if(pins.candidateId.empty()||pins.candidateId.size()>256||!hex(pins.generation)||!hex(pins.graphDigest)||!hex(pins.manifestDigest)||!hex(pins.projectionDigest))fail("accepted_manifest_pins");
 return EffectiveDatasetBinding(std::move(pins));
}
std::optional<EffectiveDatasetBinding> AcceptedDatasetRegistry::Lookup(std::string_view){return std::nullopt;}
LoadedItemSources CanonicalItemSourceLoader::LoadOffline(std::string_view g,std::string_view m,std::string_view p,OfflineSourcePins const& pins){return Load(g,m,p,pins,ProjectionDomain::OfflineContract);}
LoadedItemSources CanonicalItemSourceLoader::LoadEffectiveRealm(std::string_view g,std::string_view m,std::string_view p,EffectiveDatasetBinding const& b){return Load(g,m,p,b.pins_,ProjectionDomain::EffectiveRealm);}
LoadedItemSources CanonicalItemSourceLoader::Load(std::string_view gb,std::string_view mb,std::string_view pb,OfflineSourcePins const& pins,ProjectionDomain domain){
 V graph=parse(gb),manifest=parse(mb),projections=parse(pb);V docs=j::parse(CanonicalSchemas);
 auto validate=[&](V const& v,char const* name){auto const& root=docs.at(name);schema(v,root,root,docs);};
 validate(graph,"content-graph-v1.schema.json");validate(manifest,"content-manifest-v1.schema.json");
 if(!projections.is_array()||projections.as_array().size()>10000)fail("projection_array_bound");
 std::map<std::string,V*> conditions;for(auto& c:graph.at("condition_roots").as_array()){auto id=str(c.at("condition_id"));if(!conditions.emplace(id,&c.at("expression")).second)fail("duplicate_condition");normalizeExpression(c.at("expression"));}
 auto predicates=std::make_shared<SourcePredicateProgram>();
 std::function<std::size_t(V const&,unsigned)> retainExpression;
 retainExpression=[&](V const& value,unsigned depth)->std::size_t{
  if(depth>128)fail("condition_depth");
  SourcePredicateProgram::Expression retained;auto op=str(value.at("op"));
  using Op=SourcePredicateProgram::Op;
  retained.op=op=="true"?Op::True:op=="false"?Op::False:op=="leaf"?Op::Leaf:op=="reference"?Op::Reference:op=="not"?Op::Not:op=="all"?Op::All:op=="any"?Op::Any:Op::Unknown;
  retained.definitionDigest=digest(value);
  if(retained.op==Op::Leaf){retained.conditionType=static_cast<std::uint32_t>(num(value.at("condition_type")));for(std::size_t i=0;i<3;++i)retained.values[i]=static_cast<std::uint32_t>(num(value.at("values").as_array().at(i)));}
  if(retained.op==Op::Reference)retained.reference=str(value.at("condition_id"));
  if(retained.op==Op::Not)retained.arguments.push_back(retainExpression(value.at("argument"),depth+1));
  if(retained.op==Op::All||retained.op==Op::Any)for(auto const& argument:value.at("arguments").as_array())retained.arguments.push_back(retainExpression(argument,depth+1));
  auto index=predicates->expressions.size();predicates->expressions.push_back(std::move(retained));return index;
 };
 for(auto const& [id,value]:conditions)predicates->roots.emplace(id,retainExpression(*value,0));
 PredicateEvaluation conditionEvaluation(*predicates);
 std::map<std::string,PredicateState> conditionValue;
 for(auto const& [id,value]:conditions){(void)value;conditionValue.emplace(id,conditionEvaluation.condition(id));}
 auto availability=[&](V& a){if(num(a.at("minimum_frontier"))>num(a.at("maximum_frontier")))fail("invalid_frontier");if(!a.at("condition_id").is_null())conditionEvaluation.condition(str(a.at("condition_id")));for(auto key:{"event_ids","pool_ids"}){auto& x=a.at(key).as_array();std::sort(x.begin(),x.end(),[](V const& l,V const& r){return num(l)<num(r);});}};
 std::map<std::string,V const*> nodes;for(auto const& [kind,collection]:kinds){auto& a=graph.at("nodes").at(collection).as_array();for(auto& node:a){auto id=kind+":"+std::to_string(num(node.at("id")));if(!nodes.emplace(id,&node).second)fail("duplicate_node");availability(node.at("availability"));}std::sort(a.begin(),a.end(),[](V const& l,V const& r){return num(l.at("id"))<num(r.at("id"));});}
 // Sorting arrays moves values: rebind every pointer after normalization.
 nodes.clear();for(auto const& [kind,collection]:kinds)for(auto const& node:graph.at("nodes").at(collection).as_array())nodes.emplace(kind+":"+std::to_string(num(node.at("id"))),&node);
 std::set<std::string> edgeIds,reverse,provenance;
 for(auto& edge:graph.at("edges").as_array()){
  if(!edgeIds.insert(str(edge.at("edge_id"))).second)fail("duplicate_edge");
  for(auto endpoint:{"from","to"})if(!nodes.contains(identity(edge.at(endpoint))))fail("missing_endpoint");
  // Distinct native rows/reward slots may share endpoints and semantics.
  // Provenance stays globally unique and is part of the reverse identity.
  auto reverseId=canonical(A{edge.at("kind"),edge.at("from"),edge.at("to"),edge.at("semantics"),edge.at("provenance")});
  if(!reverse.insert(reverseId).second)fail("ambiguous_reverse_identity");
  if(!provenance.insert(canonical(edge.at("provenance"))).second)fail("ambiguous_provenance");
  availability(edge.at("availability"));
 }
 sortBy(graph.at("condition_roots").as_array(),"condition_id");sortBy(graph.at("edges").as_array(),"edge_id");
 std::map<std::string,V const*> edges;for(auto const& edge:graph.at("edges").as_array())edges.emplace(str(edge.at("edge_id")),&edge);
 std::map<std::string,std::vector<V const*>> reagentsBySource;
 for(auto const& edge:graph.at("edges").as_array())if(str(edge.at("kind"))=="recipe_reagent")reagentsBySource[identity(edge.at("from"))].push_back(&edge);
 auto gd=digest(graph);if(gd!=pins.graphDigest||str(manifest.at("graph_digest"))!=gd||str(manifest.at("reproducible_peer_digest"))!=gd)fail("graph_digest_mismatch");
 auto const& source=manifest.at("source");if(str(source.at("candidate_id"))!=pins.candidateId)fail("candidate_mismatch");
 for(auto key:{"core_revision","playerbots_revision","progression_revision","world_dump_digest","dbc_set_digest"})if(source.at(key)!=graph.at(key))fail("source_pin_mismatch:"+std::string(key));
 std::set<std::string> names;std::uint64_t ordinal=0;for(auto const& migration:manifest.at("migrations").as_array()){if(num(migration.at("ordinal"))!=ordinal++)fail("ordered_migration_ordinal");if(!names.insert(str(migration.at("name"))).second)fail("duplicate_migration");}
 names.clear();for(auto const& file:manifest.at("dbc_files").as_array())if(!names.insert(str(file.at("name"))).second)fail("duplicate_dbc_file");sortBy(manifest.at("dbc_files").as_array(),"name");
 if(digest(manifest)!=pins.manifestDigest)fail("manifest_digest_mismatch");
 V generation=O{{"source",source},{"compiler_revision",manifest.at("compiler_revision")},{"migrations",manifest.at("migrations")},{"dbc_files",manifest.at("dbc_files")},{"content_revision",graph.at("content_revision")}};
 if(digest(generation)!=pins.generation)fail("generation_mismatch");
 std::vector<ItemSourceOption> options;std::set<std::string> projectedEdges;std::set<std::uint64_t> projectedItems;std::map<std::string,std::string> blocked;
 auto availabilityBlock=[&](V const& a)->std::string{if(num(a.at("phase_mask"))!=1||num(a.at("spawn_mask"))!=1)return "mask_context_unavailable";if(!a.at("event_ids").as_array().empty())return "event_context_unavailable";if(!a.at("pool_ids").as_array().empty())return "pool_context_unavailable";auto const& c=a.at("condition_id");if(c.is_null())return {};auto state=conditionValue.at(str(c));return state==PredicateState::False?"condition_false":state==PredicateState::Unknown?"condition_unknown":"";};
 auto retainAvailability=[&](std::string const& sourceId,std::string locus,std::string locusKind,V const& owner,V const& value,
     std::optional<std::uint32_t> reagent=std::nullopt,bool projectedAvailable=true){
  SourcePredicateProgram::Availability requirement;requirement.locus=std::move(locus);requirement.locusKind=std::move(locusKind);
  requirement.ownerKind=str(owner.at("kind"));requirement.ownerId=static_cast<std::uint32_t>(num(owner.at("id")));
  if(requirement.locusKind=="reagent_edge")requirement.astPrefix="$.reagent["+j::serialize(V(requirement.locus.substr(5)))+"]";
  requirement.reagentItemId=reagent;requirement.projectedAvailable=projectedAvailable;
  if(!value.at("condition_id").is_null())requirement.conditionId=str(value.at("condition_id"));
  requirement.minimumFrontier=static_cast<std::uint32_t>(num(value.at("minimum_frontier")));
  requirement.maximumFrontier=static_cast<std::uint32_t>(num(value.at("maximum_frontier")));
  requirement.phaseMask=static_cast<std::uint32_t>(num(value.at("phase_mask")));
  requirement.spawnMask=static_cast<std::uint32_t>(num(value.at("spawn_mask")));
  for(auto const& id:value.at("event_ids").as_array())requirement.eventIds.push_back(static_cast<std::uint32_t>(num(id)));
  for(auto const& id:value.at("pool_ids").as_array())requirement.poolIds.push_back(static_cast<std::uint32_t>(num(id)));
  predicates->requirements[sourceId].push_back(std::move(requirement));
 };
 for(auto& projection:projections.as_array()){
  validate(projection,"item-source-v1.schema.json");auto item=num(projection.at("item_id"));if(!item||!projectedItems.insert(item).second)fail("duplicate_or_zero_item_projection");if(projection.at("content_revision")!=graph.at("content_revision")||str(projection.at("graph_digest"))!=gd)fail("projection_generation_mismatch");
  auto target=nodes.find("item:"+std::to_string(item));if(target==nodes.end())fail("missing_item_endpoint");
  for(auto& s:projection.at("sources").as_array()){
   auto id=str(s.at("source_id"));if(!projectedEdges.insert(id).second)fail("duplicate_reverse_entry");auto edgeIt=edges.find(id);if(edgeIt==edges.end())fail("missing_reverse_edge");auto const& edge=*edgeIt->second;
   auto kind=str(s.at("source_kind"));auto const& adapter=adapters.at(kind);
   if(str(s.at("source_ref").at("kind"))!=adapter.node||s.at("source_ref")!=edge.at("from")||str(edge.at("to").at("kind"))!="item"||num(edge.at("to").at("id"))!=item||str(edge.at("kind"))!=adapter.edge||str(edge.at("semantics"))!=adapter.semantics)fail("source_edge_semantics:"+id);
   // Explicit normalized adapter provenance; never represented as a native table/extract proof.
   auto const& prov=edge.at("provenance");
   auto const& key=prov.at("natural_key");
   bool normalized=str(prov.at("source"))=="normalized."+kind;
   bool legacy=key==V(A{s.at("source_ref").at("id"),projection.at("item_id")});
   bool qualifiedQuest=false;
   if(normalized&&(kind=="quest_reward"||kind=="dungeon_quest")&&key.as_array().size()==4){
    auto const& k=key.as_array();auto const& attrs=edge.at("attributes");
    auto category=str(k[2]);auto slot=num(k[3]);
    auto slots=category=="guaranteed"?4u:category=="choice"?6u:0u;
    qualifiedQuest=k[0]==s.at("source_ref").at("id")&&k[1]==projection.at("item_id")&&slot>0&&slot<=slots
     &&str(attrs.at("native_table"))=="quest_template"&&attrs.at("quest_id")==k[0]
     &&attrs.at("reward_kind")==k[2]&&attrs.at("sql_reward_column_suffix")==k[3]
     &&num(attrs.at("native_reward_index"))==slot-1
     &&str(attrs.at("native_item_column"))==(category=="guaranteed"?"RewardItem":"RewardChoiceItemID")+std::to_string(slot)
     &&attrs.at("minimum_count")==s.at("minimum_count")&&attrs.at("maximum_count")==s.at("maximum_count");
   }
   if(!normalized||(!legacy&&!qualifiedQuest))fail("source_natural_key:"+id);
   SourcePredicateProgram::Source retainedSource;retainedSource.kind=str(s.at("source_ref").at("kind"));retainedSource.id=static_cast<std::uint32_t>(num(s.at("source_ref").at("id")));retainedSource.itemId=static_cast<std::uint32_t>(item);
   if(qualifiedQuest)retainedSource.reward=O{{"kind",key.as_array()[2]},{"index",num(key.as_array()[3])-1},{"item_id",item}};
   predicates->sources.emplace(id,std::move(retainedSource));
   if(s.at("condition_root")!=edge.at("availability").at("condition_id"))fail("condition_identity:"+id);
   auto lo=num(s.at("minimum_phase")),hi=num(s.at("maximum_phase"));if(lo>hi)fail("invalid_phase");
   std::string reason;
   for(auto const& [locus,locusKind,owner,a]:std::vector<std::tuple<std::string,std::string,V const*,V const*>>{
       {"edge:"+id,"source_edge",&edge.at("from"),&edge.at("availability")},{"node:"+identity(edge.at("from")),"source_node",&edge.at("from"),&nodes.at(identity(edge.at("from")))->at("availability")},{"node:item:"+std::to_string(item),"item_node",&edge.at("to"),&target->second->at("availability")}}){
    retainAvailability(id,locus,locusKind,*owner,*a);lo=std::max(lo,num(a->at("minimum_frontier")));hi=std::min(hi,num(a->at("maximum_frontier")));auto r=availabilityBlock(*a);if(reason.empty()&&!r.empty())reason=r;
   }
   if(lo>hi)fail("empty_phase_intersection");
   if(num(s.at("minimum_count"))==0||num(s.at("minimum_count"))>num(s.at("maximum_count")))fail("unsupported_zero_or_invalid_yield");
   if(num(s.at("chance_numerator"))>num(s.at("chance_denominator")))fail("invalid_probability");
   ItemSourceOption option;option.stableId=id;option.itemId=static_cast<std::uint32_t>(item);option.kind=adapter.kind;option.minimumPhase=static_cast<std::uint32_t>(lo);option.maximumPhase=static_cast<std::uint32_t>(hi);option.requiredGroupSize=static_cast<std::uint32_t>(num(s.at("required_group_size")));option.available=reason.empty();
   option.minimumCount=static_cast<std::uint32_t>(num(s.at("minimum_count")));option.maximumCount=static_cast<std::uint32_t>(num(s.at("maximum_count")));
   if(!reason.empty())blocked.emplace(id,reason);
   option.chanceNumerator=static_cast<std::uint32_t>(num(s.at("chance_numerator")));option.chanceDenominator=static_cast<std::uint32_t>(num(s.at("chance_denominator")));option.moneyCostCopper=num(s.at("money_cost_copper"));option.travelSeconds=static_cast<Score>(num(s.at("travel_seconds")));option.secondsPerAttempt=static_cast<Score>(num(s.at("seconds_per_attempt")));option.riskMilli=static_cast<Score>(num(s.at("risk_milli")));option.contentionMilli=static_cast<Score>(num(s.at("contention_milli")));option.requiredProfessionSkillId=static_cast<std::uint32_t>(num(s.at("required_profession_skill_id")));option.requiredProfessionSkillValue=static_cast<std::uint32_t>(num(s.at("required_profession_skill_value")));
   sortSet(s.at("required_capabilities").as_array());for(auto const& cap:s.at("required_capabilities").as_array())option.requiredCapabilities.push_back(str(cap));if(!s.at("quest_alternative_group").is_null())option.questAlternativeGroup=str(s.at("quest_alternative_group"));
   std::map<std::uint64_t,V const*> reagentEdges;
   auto candidates=reagentsBySource.find(identity(s.at("source_ref")));
   if(candidates!=reagentsBySource.end())for(auto const* candidate:candidates->second){
    if(kind!="craft"||str(candidate->at("semantics"))!="recipe_reagent"||str(candidate->at("to").at("kind"))!="item")fail("invalid_reagent_semantics");
    auto did=num(candidate->at("to").at("id"));if(!reagentEdges.emplace(did,candidate).second)fail("ambiguous_reagent");
   }
   sortSet(s.at("craft_dependencies").as_array());
   if(kind!="craft"&&!s.at("craft_dependencies").as_array().empty())fail("noncraft_dependencies");
   std::set<std::uint64_t> dependencies;for(auto const& dep:s.at("craft_dependencies").as_array()){
    auto did=num(dep.at("item_id"));if(!dependencies.insert(did).second)fail("duplicate_craft_dependency");
    if(!nodes.contains("item:"+std::to_string(did)))fail("missing_craft_item");
    auto found=reagentEdges.find(did);if(found==reagentEdges.end())fail("missing_recipe_reagent_edge");auto const& reagent=*found->second;
    auto const* count=reagent.at("attributes").as_object().if_contains("count");if(!count||num(*count)!=num(dep.at("count")))fail("recipe_reagent_count");
    auto const& rp=reagent.at("provenance");if(str(rp.at("source"))!="normalized.recipe_reagent"||rp.at("natural_key")!=V(A{s.at("source_ref").at("id"),dep.at("item_id")}))fail("recipe_reagent_natural_key");
    auto depLo=static_cast<std::uint64_t>(option.minimumPhase),depHi=static_cast<std::uint64_t>(option.maximumPhase);bool depAvailable=dep.at("available").as_bool();
    for(auto const& [locus,a]:std::vector<std::pair<std::string,V const*>>{
        {"edge:"+str(reagent.at("edge_id")),&reagent.at("availability")},{"node:item:"+std::to_string(did),&nodes.at("item:"+std::to_string(did))->at("availability")}}){
     retainAvailability(id,locus,a==&reagent.at("availability")?"reagent_edge":"reagent_node",a==&reagent.at("availability")?reagent.at("from"):reagent.at("to"),*a,static_cast<std::uint32_t>(did),dep.at("available").as_bool());
     depLo=std::max(depLo,num(a->at("minimum_frontier")));depHi=std::min(depHi,num(a->at("maximum_frontier")));auto r=availabilityBlock(*a);if(!r.empty()){depAvailable=false;blocked.emplace(id,"craft_dependency:"+r);}
    }
    if(depLo<=depHi){option.minimumPhase=static_cast<std::uint32_t>(depLo);option.maximumPhase=static_cast<std::uint32_t>(depHi);}
    else {option.phaseAuthorized=false;blocked.emplace(id,"craft_dependency:empty_phase_intersection");}
    option.craftDependencies.push_back({static_cast<std::uint32_t>(did),static_cast<std::uint32_t>(num(dep.at("count"))),depAvailable,dep.at("phase_authorized").as_bool()&&depLo<=depHi,true});
   }
   if(dependencies.size()!=reagentEdges.size())fail("omitted_recipe_reagent");
   std::sort(option.craftDependencies.begin(),option.craftDependencies.end(),[](auto const& l,auto const& r){return l.itemId<r.itemId;});options.push_back(std::move(option));
  }sortBy(projection.at("sources").as_array(),"source_id");
 }
 std::sort(projections.as_array().begin(),projections.as_array().end(),[](V const& l,V const& r){return num(l.at("item_id"))<num(r.at("item_id"));});
 // A cyclic reagent contract cannot establish earned acquisition, even offline.
 std::map<std::uint32_t,std::set<std::uint32_t>> reagentGraph;
 for(auto const& option:options)if(option.kind==ItemSourceKind::Craft)for(auto const& dep:option.craftDependencies)reagentGraph[option.itemId].insert(dep.itemId);
 std::map<std::uint32_t,unsigned> reagentState;
 std::function<void(std::uint32_t,unsigned)> visitReagent=[&](std::uint32_t item,unsigned depth){
  if(depth>128)fail("recipe_dependency_depth");
  if(reagentState[item]==1)fail("recipe_dependency_cycle");
  if(reagentState[item]==2)return;
  reagentState[item]=1;for(auto next:reagentGraph[item])visitReagent(next,depth+1);reagentState[item]=2;
 };
 for(auto const& option:options)visitReagent(option.itemId,0);
 if(digest(projections)!=pins.projectionDigest)fail("projection_digest_mismatch");
 retainQueries(*predicates,pins);
 return LoadedItemSources(ItemSourceIndex(str(graph.at("content_revision")),gd,std::move(options),domain==ProjectionDomain::EffectiveRealm),pins,domain,std::move(blocked),str(graph.at("core_revision")),str(graph.at("playerbots_revision")),std::move(predicates));
}
std::string CanonicalGearPlanningService::ActorQueries(LoadedItemSources const& loaded,std::vector<ItemUpgradeTarget> const& targets){
 return j::serialize(queryEnvelope(*loaded.predicates_,loaded.Pins(),sourceClosure(loaded.Index(),targets)));
}
CanonicalGearPlanResult CanonicalGearPlanningService::PlanOffline(LoadedItemSources const& loaded,BotProgressSnapshot const& snapshot,CharacterResourceSnapshot const& resources,std::vector<ItemUpgradeTarget> const& targets,GearScoringPolicy policy,std::string_view actorContextJson){
 if(loaded.Domain()!=ProjectionDomain::OfflineContract)fail("offline_domain_required");
 EarnedGearPlanner planner(std::move(policy));
 if(snapshot.coreRevision!=loaded.coreRevision_||snapshot.playerbotsRevision!=loaded.playerbotsRevision_){
  GearPlan rejected=planner.MakePlanMetadata(snapshot);rejected.errors.push_back("source_revision_mismatch");return {ProjectionDomain::OfflineContract,loaded.Pins(),loaded.BlockedReasons(),std::move(rejected)};
 }
 if(actorContextJson.empty())return {ProjectionDomain::OfflineContract,loaded.Pins(),loaded.BlockedReasons(),planner.Evaluate(snapshot,resources,loaded.Index(),targets)};
 auto closure=sourceClosure(loaded.Index(),targets);auto envelope=queryEnvelope(*loaded.predicates_,loaded.Pins(),closure);
 auto states=contextVerdicts(actorContextJson,envelope,snapshot);
 auto [options,blocked]=evaluateSources(*loaded.predicates_,loaded.Index(),loaded.BlockedReasons(),closure,states);
 ItemSourceIndex index(loaded.Index().ContentRevision(),loaded.Index().ContentGraphDigest(),std::move(options),false);
 return {ProjectionDomain::OfflineContract,loaded.Pins(),std::move(blocked),planner.Evaluate(snapshot,resources,index,targets)};
}
CanonicalGearPlanResult CanonicalGearPlanningService::PlanEffectiveRealm(LoadedItemSources const& loaded,BotProgressSnapshot const& snapshot,CharacterResourceSnapshot const& resources,std::vector<ItemUpgradeTarget> const& targets,GearScoringPolicy policy,std::string_view actorContextJson){
 if(loaded.Domain()!=ProjectionDomain::EffectiveRealm || !loaded.Index().IsVerifiedCanonicalProjection())fail("effective_realm_required");
 EarnedGearPlanner planner(std::move(policy));
 if(snapshot.coreRevision!=loaded.coreRevision_||snapshot.playerbotsRevision!=loaded.playerbotsRevision_){
  GearPlan rejected=planner.MakePlanMetadata(snapshot);rejected.errors.push_back("source_revision_mismatch");return {ProjectionDomain::EffectiveRealm,loaded.Pins(),loaded.BlockedReasons(),std::move(rejected)};
 }
 // Configured source-work ceiling, not a measured latency guarantee. Count repeats exactly.
 constexpr std::size_t MaxEvaluationWork=16384;
 std::size_t evaluationWork=0;
 for(auto const& target:targets){
  auto count=loaded.Index().SourcesForItem(target.itemId).size();
  if(count>MaxEvaluationWork-evaluationWork)fail("evaluation_work_bound");
  evaluationWork+=count;
 }
 if(actorContextJson.empty())return {ProjectionDomain::EffectiveRealm,loaded.Pins(),loaded.BlockedReasons(),planner.Plan(snapshot,resources,loaded.Index(),targets)};
 auto closure=sourceClosure(loaded.Index(),targets);auto envelope=queryEnvelope(*loaded.predicates_,loaded.Pins(),closure);
 auto states=contextVerdicts(actorContextJson,envelope,snapshot);
 auto [options,blocked]=evaluateSources(*loaded.predicates_,loaded.Index(),loaded.BlockedReasons(),closure,states);
 ItemSourceIndex index(loaded.Index().ContentRevision(),loaded.Index().ContentGraphDigest(),std::move(options),true);
 return {ProjectionDomain::EffectiveRealm,loaded.Pins(),std::move(blocked),planner.Plan(snapshot,resources,index,targets)};
}

}
