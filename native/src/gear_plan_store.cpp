#include "pbguide/gear_plan_store.hpp"
#include "pbguide/gear_plan_json.hpp"
#include "json_contract.hpp"
#include <boost/json.hpp>
#include <atomic>
#include <cerrno>
#include <fcntl.h>
#include <map>
#include <regex>
#include <sys/stat.h>
#include <tuple>
#include <unistd.h>
#include <utility>
namespace pbguide { namespace {
namespace j = boost::json;
// The complete envelope, including checksum/pins, shares the JSON parser bound.
constexpr std::size_t MaxBytes = json_contract::MaxDocumentBytes;
[[noreturn]] void Fail(char const* code) { throw GearPlanStoreError(code); }
[[noreturn]] void FileError(char const* code, char const* operation, int error) {
    throw GearPlanStoreError(code, std::string(operation) + "_errno_" + std::to_string(error));
}
[[noreturn]] void Io(char const* operation, int error = errno) {
    FileError("io_error", operation, error);
}
class Fd {
    int fd_;
public:
    explicit Fd(int fd):fd_(fd) {}
    ~Fd(){if(fd_>=0)::close(fd_);}
    Fd(Fd const&)=delete;
    Fd& operator=(Fd const&)=delete;
    int get() const { return fd_; }
    void CloseChecked(char const* operation) {
        int fd=fd_;
        fd_=-1;
        if(::close(fd)<0)
            Io(operation);
    }
};
int OpenDirectory(std::string const& path) {
    if(path.empty() || path.front()!='/' || path.back()=='/' || path.find('\0')!=std::string::npos)
        Fail("unsafe_store");
    Fd root(::open("/",O_RDONLY|O_DIRECTORY|O_CLOEXEC));
    if(root.get()<0)
        Io("root_open");
    int current=::dup(root.get());
    if(current<0)
        Io("dup");
    std::size_t start=1;
    try {
        while(start<path.size()) {
            auto end=path.find('/',start);
            auto part=path.substr(start,end-start);
            if(part.empty()||part=="."||part=="..")
                Fail("unsafe_store");
            int next=::openat(current,part.c_str(),O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);
            if(next<0) {
                int error=errno;
                FileError(error==ENOTDIR||error==ELOOP||error==ENOENT ? "unsafe_store" : "io_error",
                    "directory_open",error);
            }
            int previous=current;
            current=next;
            if(::close(previous)<0)
                Io("directory_close");
            if(end==std::string::npos)
                break;
            start=end+1;
        }
        struct stat st{};
        if(::fstat(current,&st)<0)
            Io("directory_fstat");
        if(!S_ISDIR(st.st_mode)||st.st_uid!=::geteuid()||(st.st_mode&07777)!=0700)
            Fail("unsafe_store");
        root.CloseChecked("root_close");
        return current;
    } catch(...) {
        ::close(current);
        throw;
    }
}
void SafeFile(struct stat const& st) {
    if(!S_ISREG(st.st_mode)||st.st_uid!=::geteuid()||st.st_nlink!=1||(st.st_mode&07777)!=0600)
        Fail("unsafe_store");
    if(st.st_size<0||static_cast<std::uint64_t>(st.st_size)>MaxBytes)
        Fail("invalid_record");
}
std::string Name(std::string_view id) {
    if(id.size()>160||!std::regex_match(id.begin(),id.end(),std::regex("gear_plan:[0-9]+:[0-9]+")))
        Fail("invalid_context");
    return std::string(id)+".json";
}
std::string Read(int dir,std::string const& name) {
    Fd fd(::openat(dir,name.c_str(),O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC));
    if(fd.get()<0) {
        int error=errno;
        FileError(error==ENOENT ? "not_found" : error==ELOOP||error==ENOTDIR ? "unsafe_store" : "io_error",
            "record_open",error);
    }
    struct stat st{};
    if(::fstat(fd.get(),&st)<0)
        Io("record_fstat");
    SafeFile(st);
    std::string bytes;
    char buffer[8192];
    for(;;) {
        ssize_t n=::read(fd.get(),buffer,sizeof buffer);
        if(n<0) {
            if(errno==EINTR)
                continue;
            Io("record_read");
        }
        if(n==0)
            break;
        if(static_cast<std::size_t>(n)>MaxBytes-bytes.size())
            Fail("invalid_record");
        bytes.append(buffer,static_cast<std::size_t>(n));
    }
    struct stat after{};
    if(::fstat(fd.get(),&after)<0)
        Io("record_fstat");
    SafeFile(after);
    if(after.st_size!=static_cast<off_t>(bytes.size()))
        Fail("invalid_record");
    fd.CloseChecked("record_close");
    return bytes;
}
void Closed(j::value const& v,std::initializer_list<char const*> keys) {
    if(!v.is_object()||v.as_object().size()!=keys.size())
        Fail("invalid_record");
    for(auto key:keys)
        if(!v.as_object().contains(key))
            Fail("invalid_record");
}
std::string Text(j::value const& value) {
    if(!value.is_string())
        Fail("invalid_record");
    return std::string(value.as_string());
}
j::object PinObject(OfflineSourcePins const& p) {
    return {{"candidate_id",p.candidateId},{"generation",p.generation},{"graph_digest",p.graphDigest},
        {"manifest_digest",p.manifestDigest},{"projection_digest",p.projectionDigest}};
}
std::string Checksum(j::value const& payload) {
    // Fixed domain separator, sorted object keys, exact integers, ordered arrays.
    return json_contract::Digest(j::object{{"digest_domain","cpi-211-offline-gear-plan-store-v1"},{"payload",payload}});
}
std::string Encode(CanonicalGearPlanResult const& r) {
    if(r.domain!=ProjectionDomain::OfflineContract)
        Fail("invalid_context");
    j::object blocked;
    for(auto const& [id,reason]:r.blockedReasons)
        blocked[id]=reason;
    j::object payload{{"schema_version","cpi-211-offline-gear-plan-store-v1"},{"domain","offline_contract"},
        {"provenance",PinObject(r.provenance)},{"blocked_reasons",std::move(blocked)},
        {"plan",json_contract::Parse(GearPlanToJson(r.plan))}};
    auto sum=Checksum(payload);
    payload["checksum"]=sum;
    auto bytes=json_contract::Canonical(payload);
    if(bytes.size()>MaxBytes)
        Fail("invalid_record");
    return bytes;
}
CanonicalGearPlanResult Decode(std::string_view bytes) {
    try {
        auto v=json_contract::Parse(bytes);
        Closed(v,{"schema_version","domain","provenance","blocked_reasons","plan","checksum"});
        if(Text(v.at("schema_version"))!="cpi-211-offline-gear-plan-store-v1"||Text(v.at("domain"))!="offline_contract")
            Fail("invalid_record");
        auto checksum=Text(v.at("checksum"));
        v.as_object().erase("checksum");
        if(Checksum(v)!=checksum)
            Fail("invalid_record");
        auto const& pins=v.at("provenance");
        Closed(pins,{"candidate_id","generation","graph_digest","manifest_digest","projection_digest"});
        CanonicalGearPlanResult r{ProjectionDomain::OfflineContract,
            {Text(pins.at("candidate_id")),Text(pins.at("generation")),Text(pins.at("graph_digest")),
                Text(pins.at("manifest_digest")),Text(pins.at("projection_digest"))},{},
            GearPlanFromJson(j::serialize(v.at("plan")))};
        auto stable=[](std::string const& value) {
            return value.size()<=160&&std::regex_match(value,std::regex("[a-z][a-z0-9_-]*:[a-zA-Z0-9_.:-]+"));
        };
        if(!stable(r.provenance.candidateId))
            Fail("invalid_record");
        for(auto const* pin:{&r.provenance.generation,&r.provenance.graphDigest,
                &r.provenance.manifestDigest,&r.provenance.projectionDigest})
            if(!std::regex_match(*pin,std::regex("[0-9a-f]{64}")))
                Fail("invalid_record");
        if(r.plan.contentGraphDigest!=r.provenance.graphDigest)
            Fail("invalid_record");
        if(!v.at("blocked_reasons").is_object())
            Fail("invalid_record");
        for(auto const& kv:v.at("blocked_reasons").as_object()) {
            auto id=std::string(kv.key()),reason=Text(kv.value());
            if(!stable(id)||reason.size()>240||!std::regex_match(reason,std::regex("[a-z][a-z0-9_.-]*(:[a-zA-Z0-9_.:-]+)?")))
                Fail("invalid_record");
            r.blockedReasons.emplace(std::move(id),std::move(reason));
        }
        return r;
    } catch(GearPlanStoreError const&) {
        throw;
    } catch(std::exception const&) {
        Fail("invalid_record");
    }
}
void Context(LoadedItemSources const& loaded,BotProgressSnapshot const& s,GearScoringPolicy const& policy) {
    try {
        EarnedGearPlanner validatePolicy(policy);
        (void)validatePolicy;
    } catch(std::exception const&) {
        Fail("invalid_context");
    }
    if(loaded.Domain()!=ProjectionDomain::OfflineContract||loaded.Index().IsVerifiedCanonicalProjection()||
        !IsValidBotProgressSnapshot(s)||!IsValidBotProgressSnapshotIdentity(s)||
        s.coreRevision!=loaded.CoreRevision()||s.playerbotsRevision!=loaded.PlayerbotsRevision()||
        s.contentRevision!=loaded.Index().ContentRevision()||s.contentGraphDigest!=loaded.Pins().graphDigest)
        Fail("invalid_context");
}
void Binding(CanonicalGearPlanResult const& r,std::string_view id,LoadedItemSources const& loaded,
    BotProgressSnapshot const& s,GearScoringPolicy const& policy) {
    if(r.domain!=ProjectionDomain::OfflineContract||r.provenance!=loaded.Pins()||r.blockedReasons!=loaded.BlockedReasons()||
        r.plan.planId!=id||r.plan.characterId!=s.characterId||r.plan.contentRevision!=s.contentRevision||
        r.plan.contentGraphDigest!=loaded.Pins().graphDigest||r.plan.policyVersion!=policy.version||
        r.plan.policyIdentity!=CanonicalGearPolicyIdentity(policy))
        Fail("invalid_context");
    std::map<std::string,ItemSourceOption const*> sourceItems;
    for(auto const& source:loaded.Index().Sources())
        sourceItems.emplace(source.stableId,&source);
    GearSourceEvaluation const* best=nullptr;
    for(auto const& evaluation:r.plan.evaluations) {
        auto source=sourceItems.find(evaluation.stableId);
        if(source==sourceItems.end()||source->second->itemId!=evaluation.itemId)
            Fail("invalid_context");
        if(evaluation.eligible) {
            auto const& option=*source->second;
            if(!option.available||!option.phaseAuthorized||!option.earnedSource||
                loaded.BlockedReasons().contains(evaluation.stableId))
                Fail("invalid_context");
            for(auto const& dependency:option.craftDependencies)
                if(!dependency.available||!dependency.phaseAuthorized||!dependency.earnedSource)
                    Fail("invalid_context");
        }
        if(evaluation.eligible && (!best || evaluation.score.total>best->score.total ||
            (evaluation.score.total==best->score.total && std::tie(evaluation.targetId,evaluation.stableId)<
                std::tie(best->targetId,best->stableId))))
            best=&evaluation;
    }
    bool shouldSelect=best && best->score.total>=policy.minimumPursuitScore;
    if(shouldSelect!=r.plan.selectedTargetId.has_value())
        Fail("invalid_context");
}
CanonicalGearPlanResult LoadAt(int dir,std::string_view id,LoadedItemSources const& loaded,
    BotProgressSnapshot const& s,GearScoringPolicy const& policy) {
    auto r=Decode(Read(dir,Name(id)));
    Binding(r,id,loaded,s,policy);
    return r;
}
}
OfflineGearPlanStore::OfflineGearPlanStore(std::string path):directory_(std::move(path)) {
    Fd checked(OpenDirectory(directory_));
}
CanonicalGearPlanResult OfflineGearPlanStore::PlanAndSave(LoadedItemSources const& loaded,
    BotProgressSnapshot const& snapshot,CharacterResourceSnapshot const& resources,
    std::vector<ItemUpgradeTarget> const& targets,GearScoringPolicy policy) const {
    Context(loaded,snapshot,policy);
    auto result=CanonicalGearPlanningService::PlanOffline(loaded,snapshot,resources,targets,policy);
    Save(result,loaded,snapshot,policy);
    return result;
}
CanonicalGearPlanResult OfflineGearPlanStore::PlanAndSaveOfflineSources(std::string_view graphBytes,
    std::string_view manifestBytes,std::string_view projectionBytes,OfflineSourcePins const& pins,
    BotProgressSnapshot const& snapshot,CharacterResourceSnapshot const& resources,
    std::vector<ItemUpgradeTarget> const& targets,GearScoringPolicy policy) const {
    auto loaded=CanonicalItemSourceLoader::LoadOffline(graphBytes,manifestBytes,projectionBytes,pins);
    return PlanAndSave(loaded,snapshot,resources,targets,policy);
}
CanonicalGearPlanResult OfflineGearPlanStore::Load(std::string_view id,LoadedItemSources const& loaded,
    BotProgressSnapshot const& snapshot,GearScoringPolicy policy) const {
    Context(loaded,snapshot,policy);
    Fd dir(OpenDirectory(directory_));
    return LoadAt(dir.get(),id,loaded,snapshot,policy);
}
void OfflineGearPlanStore::Save(CanonicalGearPlanResult const& result,LoadedItemSources const& loaded,
    BotProgressSnapshot const& snapshot,GearScoringPolicy policy) const {
    Context(loaded,snapshot,policy);
    Binding(result,result.plan.planId,loaded,snapshot,policy);
    std::string bytes;
    try {
        bytes=Encode(result);
    } catch(GearPlanStoreError const&) {
        throw;
    } catch(std::exception const&) {
        Fail("invalid_record");
    }
    auto name=Name(result.plan.planId);
    Fd dir(OpenDirectory(directory_));
    // Existing unsafe/corrupt records must never be silently reset or overwritten.
    try {
        auto prior=Decode(Read(dir.get(),name));
        if(prior.plan.planId!=result.plan.planId)
            Fail("invalid_record");
    } catch(GearPlanStoreError const& e) {
        if(e.Code()!="not_found")
            throw;
    }
    static std::atomic<unsigned long long> sequence{0};
    std::string temporary;
    int raw=-1;
    for(unsigned attempt=0;attempt<100;++attempt) {
        temporary=".gear-plan."+std::to_string(::getpid())+"."+std::to_string(sequence++)+".part";
        raw=::openat(dir.get(),temporary.c_str(),O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);
        if(raw>=0)
            break;
        if(errno!=EEXIST)
            Io("temp_open");
    }
    if(raw<0)
        Io("temp_open",EEXIST);
    Fd fd(raw);
    struct stat owned{};
    bool renamed=false;
    auto cleanup=[&]() {
        struct stat now{};
        if(!renamed&&owned.st_ino!=0&&::fstatat(dir.get(),temporary.c_str(),&now,AT_SYMLINK_NOFOLLOW)==0&&
            now.st_dev==owned.st_dev&&now.st_ino==owned.st_ino)
            ::unlinkat(dir.get(),temporary.c_str(),0);
    };
    try {
        if(::fstat(fd.get(),&owned)<0)
            Io("temp_fstat");
        if(::fchmod(fd.get(),0600)<0)
            Io("temp_fchmod");
        struct stat checked{};
        if(::fstat(fd.get(),&checked)<0)
            Io("temp_fstat");
        SafeFile(checked);
        std::size_t offset=0;
        while(offset<bytes.size()) {
            auto n=::write(fd.get(),bytes.data()+offset,bytes.size()-offset);
            if(n<0) {
                if(errno==EINTR)
                    continue;
                Io("temp_write");
            }
            if(n==0)
                Io("temp_write",EIO);
            offset+=static_cast<std::size_t>(n);
        }
        while(::fsync(fd.get())<0) {
            if(errno==EINTR)
                continue;
            Io("temp_fsync");
        }
        fd.CloseChecked("temp_close");
        if(::renameat(dir.get(),temporary.c_str(),dir.get(),name.c_str())<0)
            Io("record_rename");
        renamed=true;
        while(::fsync(dir.get())<0) {
            if(errno==EINTR)
                continue;
            Io("directory_fsync");
        }
        auto restored=LoadAt(dir.get(),result.plan.planId,loaded,snapshot,policy);
        if(restored!=result || Encode(restored)!=bytes)
            throw GearPlanStoreError("invalid_record", "readback_mismatch");
    } catch(std::exception const& error) {
        cleanup();
        if(renamed)
            throw GearPlanStoreError("commit_uncertain",error.what());
        throw;
    } catch(...) {
        cleanup();
        if(renamed)
            throw GearPlanStoreError("commit_uncertain","unknown_failure_after_rename");
        throw;
    }
}
}
