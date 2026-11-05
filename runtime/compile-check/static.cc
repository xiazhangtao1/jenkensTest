#include "check.pb.h"
#include <iostream>
int main() {
    xran::check::Record original, decoded;
    original.set_value("static-protobuf");
    if (!decoded.ParseFromString(original.SerializeAsString()) || decoded.value() != "static-protobuf") return 1;
    std::cout << decoded.value() << '\n';
    return 0;
}
