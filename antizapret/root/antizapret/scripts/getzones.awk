#@include "config/exclude-regexp-dist.awk"

# Skipping empty strings
(!$1) {next}

# Skipping IP addresses
(/^([0-9]{1,3}\.){3}[0-9]{1,3}$/) {next}

# Removing leading "www."
{sub(/^www\./, "", $1)}

# Removing ending dot
{sub(/\.$/, "", $1)}

{print $1}
