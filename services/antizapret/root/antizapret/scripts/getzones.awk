BEGIN {RS="\n|\r"}

# Remove comments
{sub(/#.*$/, "", $1)}

#Strip spaces
{sub(/\s/, "", $1)}

#Strip double qoutes
{sub(/"/, "", $1)}

# Skipping empty strings
(!$1) {next}

@include "config/exclude-regexp-dist.awk"

# Skipping IP addresses
(/^([0-9]{1,3}\.){3}[0-9]{1,3}$/) {next}

# Removing leading "www."
{sub(/^www\./, "", $1)}

# Removing ending dot
{sub(/\.$/, "", $1)}

{print $1}
