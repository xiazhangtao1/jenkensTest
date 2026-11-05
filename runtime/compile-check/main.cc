#include "check.pb.h"
#include <boost/version.hpp>
#include <cpprest/json.h>
#include <etcd/Client.hpp>
// cpprestsdk's U(x) convenience macro collides with template parameter U in other SDK headers.
#ifdef U
#undef U
#endif
#include <grpcpp/grpcpp.h>
#include <nlohmann/json.hpp>
#include <spdlog/spdlog.h>
#include <opentelemetry/sdk/trace/tracer_provider.h>
#include <opentelemetry/exporters/otlp/otlp_grpc_exporter_factory.h>
#include <opentelemetry/exporters/otlp/otlp_http_exporter_factory.h>
#include <rte_version.h>
#include <rohc/rohc.h>
#include <libwebsockets.h>
#include <protobuf-c/protobuf-c.h>
#include <dlfcn.h>
#include <iostream>
#include <memory>
#include <vector>

int main() {
    xran::check::Record original, decoded;
    original.set_value("xran-sdk");
    if (!decoded.ParseFromString(original.SerializeAsString()) || decoded.value() != "xran-sdk") return 1;
    nlohmann::json report = {{"protobuf", GOOGLE_PROTOBUF_VERSION}, {"boost", BOOST_LIB_VERSION},
        {"dpdk", rte_version()}, {"websockets", lws_get_library_version()},
        {"protobuf_c", protobuf_c_version()}};
    auto json = web::json::value::parse("{\"sdk\":true}");
    if (!json.at("sdk").as_bool()) return 2;
    etcd::Client client("http://127.0.0.1:2379");
    auto channel = grpc::CreateChannel("127.0.0.1:1", grpc::InsecureChannelCredentials());
    opentelemetry::sdk::trace::TracerProvider provider(
        std::vector<std::unique_ptr<opentelemetry::sdk::trace::SpanProcessor>>{});
    auto grpc_exporter = opentelemetry::exporter::otlp::OtlpGrpcExporterFactory::Create();
    auto http_exporter = opentelemetry::exporter::otlp::OtlpHttpExporterFactory::Create();
    void *airtracer = dlopen("libairtracer.so", RTLD_NOW | RTLD_LOCAL);
    if (!airtracer) { std::cerr << dlerror() << '\n'; return 3; }
    dlclose(airtracer);
    spdlog::info("SDK check passed: {}", report.dump());
    return 0;
}
