#include <libxml++/libxml++.h>
int main() {
    xmlpp::DomParser parser;
    parser.parse_memory("<xtue>ready</xtue>");
    return !parser.get_document() || !parser.get_document()->get_root_node();
}
