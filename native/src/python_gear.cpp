#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include "pbguide/canonical_item_sources.hpp"
#include "pbguide/gear_plan_json.hpp"
#include "pbguide/replay.hpp"
#include "json_contract.hpp"
#include <boost/json.hpp>
#include <limits>
#include <memory>
#include <stdexcept>

namespace {
namespace j=boost::json;
constexpr char CapsuleName[]="pbguide.effective_gear.v1";
struct Handle { pbguide::LoadedItemSources loaded; std::string projection; };
void destroy(PyObject* capsule) { delete static_cast<Handle*>(PyCapsule_GetPointer(capsule,CapsuleName)); }
void require(bool good,char const* reason) { if(!good)throw std::invalid_argument(reason); }
std::string text(j::value const& v) { auto const& s=v.as_string();require(s.size()<=4096,"string_bound");return std::string(s); }
std::uint32_t u32(j::value const& v) { auto n=pbguide::json_contract::Unsigned(v);require(n<=std::numeric_limits<std::uint32_t>::max(),"uint32_range");return static_cast<std::uint32_t>(n); }
j::value parse(std::string_view bytes,std::size_t bound) { require(bytes.size()<=bound,"request_byte_bound");return pbguide::json_contract::Parse(bytes); }
void keys(j::object const& o,std::initializer_list<char const*> names) {
 require(o.size()==names.size(),"object_fields");for(auto n:names)require(o.contains(n),"object_fields");
}
std::vector<pbguide::ItemUpgradeTarget> parseTargets(std::string_view bytes) {
 auto targetValue=parse(bytes,1024*1024);auto const& ta=targetValue.as_array();require(ta.size()<=4096,"target_bound");
  std::vector<pbguide::ItemUpgradeTarget> targets;targets.reserve(ta.size());
  for(auto const& value:ta) {
   auto const& o=value.as_object();keys(o,{"stable_id","item_id","slot","playerbots_upgrade_score","expected_replacement_level","profession_value"});
   targets.push_back({text(o.at("stable_id")),u32(o.at("item_id")),u32(o.at("slot")),pbguide::json_contract::Signed(o.at("playerbots_upgrade_score")),u32(o.at("expected_replacement_level")),pbguide::json_contract::Signed(o.at("profession_value"))});
  }
 return targets;
}
PyObject* error() {
 try { throw; } catch(std::bad_alloc const&) {return PyErr_NoMemory();}
 catch(std::exception const& e) {PyErr_SetString(PyExc_ValueError,e.what());return nullptr;}
 catch(...) {PyErr_SetString(PyExc_RuntimeError,"gear_binding_failure");return nullptr;}
}
PyObject* load(PyObject*,PyObject* args) {
 char const *g,*m,*p,*a,*sha;Py_ssize_t gn,mn,pn,an,sn;
 if(!PyArg_ParseTuple(args,"s#s#s#s#s#",&g,&gn,&m,&mn,&p,&pn,&a,&an,&sha,&sn))return nullptr;
 try {
  auto accepted=pbguide::EffectiveDatasetBinding::FromRootAcceptedManifest({a,static_cast<std::size_t>(an)},{sha,static_cast<std::size_t>(sn)});
  auto loaded=pbguide::CanonicalItemSourceLoader::LoadEffectiveRealm({g,static_cast<std::size_t>(gn)},{m,static_cast<std::size_t>(mn)},{p,static_cast<std::size_t>(pn)},accepted);
  auto h=std::make_unique<Handle>(Handle{std::move(loaded),std::string(p,static_cast<std::size_t>(pn))});
  PyObject* result=PyCapsule_New(h.get(),CapsuleName,destroy);if(result)h.release();return result;
 } catch(...) { return error(); }
}
PyObject* sources(PyObject*,PyObject* args) {
 PyObject* capsule;if(!PyArg_ParseTuple(args,"O",&capsule))return nullptr;
 auto* h=static_cast<Handle*>(PyCapsule_GetPointer(capsule,CapsuleName));if(!h)return nullptr;
 return PyUnicode_DecodeUTF8(h->projection.data(),static_cast<Py_ssize_t>(h->projection.size()),"strict");
}
PyObject* plan(PyObject*,PyObject* args) {
 PyObject* capsule;char const *s,*r,*t,*context="";Py_ssize_t sn,rn,tn,contextSize=0;
 if(!PyArg_ParseTuple(args,"Os#s#s#|s#",&capsule,&s,&sn,&r,&rn,&t,&tn,&context,&contextSize))return nullptr;
 auto* h=static_cast<Handle*>(PyCapsule_GetPointer(capsule,CapsuleName));if(!h)return nullptr;
 try {
  require(sn<=256*1024,"snapshot_byte_bound");
  auto snapshot=pbguide::ParseGearSnapshot({s,static_cast<std::size_t>(sn)});
  require(snapshot.capabilities.size()<=128,"capability_bound");
  auto resourcesValue=parse({r,static_cast<std::size_t>(rn)},256*1024);auto const& ro=resourcesValue.as_object();
  keys(ro,{"money_copper","profession_skill_values","consumed_quest_alternative_groups","item_counts","inventory_complete"});
  pbguide::CharacterResourceSnapshot resources;resources.moneyCopper=pbguide::json_contract::Unsigned(ro.at("money_copper"));
  resources.inventoryComplete=ro.at("inventory_complete").as_bool();
  auto const& inventory=ro.at("item_counts").as_array();require(inventory.size()<=256,"inventory_bound");
  for(auto const& value:inventory) {auto const& item=value.as_object();keys(item,{"item_id","count"});auto id=u32(item.at("item_id"));require(id!=0&&resources.itemCounts.emplace(id,u32(item.at("count"))).second,"duplicate_or_zero_inventory_item");}
  auto const& skills=ro.at("profession_skill_values").as_array();require(skills.size()<=64,"profession_bound");
  for(auto const& value:skills) {auto const& skill=value.as_object();keys(skill,{"skill_id","value"});auto id=u32(skill.at("skill_id"));require(id!=0&&resources.professionSkillValues.emplace(id,u32(skill.at("value"))).second,"duplicate_or_zero_skill");}
  auto const& groups=ro.at("consumed_quest_alternative_groups").as_array();require(groups.size()<=4096,"alternative_bound");
  for(auto const& value:groups) {auto group=text(value);require(!group.empty()&&resources.consumedQuestAlternativeGroups.insert(group).second,"duplicate_or_empty_alternative");}
  auto targets=parseTargets({t,static_cast<std::size_t>(tn)});
  auto result=pbguide::CanonicalGearPlanningService::PlanEffectiveRealm(h->loaded,snapshot,resources,targets,{}, {context,static_cast<std::size_t>(contextSize)});
  auto bytes=pbguide::GearPlanToJson(result.plan);
  return PyUnicode_DecodeUTF8(bytes.data(),static_cast<Py_ssize_t>(bytes.size()),"strict");
 } catch(...) { return error(); }
}
PyObject* actorQueries(PyObject*,PyObject* args) {
 PyObject* capsule;char const* targetBytes;Py_ssize_t targetSize;
 if(!PyArg_ParseTuple(args,"Os#",&capsule,&targetBytes,&targetSize))return nullptr;
 auto* h=static_cast<Handle*>(PyCapsule_GetPointer(capsule,CapsuleName));if(!h)return nullptr;
 try {
  auto targets=parseTargets({targetBytes,static_cast<std::size_t>(targetSize)});
  auto bytes=pbguide::CanonicalGearPlanningService::ActorQueries(h->loaded,targets);
  return PyUnicode_DecodeUTF8(bytes.data(),static_cast<Py_ssize_t>(bytes.size()),"strict");
 }catch(...){return error();}
}
PyMethodDef methods[]={
 {"load",load,METH_VARARGS,"Load a hash-authenticated Root-accepted effective source capsule."},
 {"plan",plan,METH_VARARGS,"Plan using the existing C++ earned-gear planner; no effects."},
 {"actor_queries",actorQueries,METH_VARARGS,"Export authenticated actor predicate dependencies for targets and recursive craft sources."},
 {"sources",sources,METH_VARARGS,"Return the validated projection JSON, including source references."},
 {nullptr,nullptr,0,nullptr}};
PyModuleDef module={PyModuleDef_HEAD_INIT,"_pbguide_gear","Synchronous, value-only gear planning; GIL retained.",-1,methods,nullptr,nullptr,nullptr,nullptr};
}
PyMODINIT_FUNC PyInit__pbguide_gear() {return PyModule_Create(&module);}
