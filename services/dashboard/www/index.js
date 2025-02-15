const servicesUrl= "/services.json"

let currentHost = window.location.hostname;
let tabContainer = document.getElementById('tabContainer');
let contentWrapper = document.getElementById('contentWrapper');

let allTabs = [];
let allContents = [];
let serviceHashes = [];

function activateTab(index) {
    allTabs.forEach(tab => tab.classList.remove('active'));
    allContents.forEach(cont => cont.classList.remove('active'));

    if (index >= 0 && index < allTabs.length) {
        allTabs[index].classList.add('active');
        allContents[index].classList.add('active');
        window.location.hash = serviceHashes[index];
    }
}

function createTab(serviceName, serviceUrl, hashValue) {
    let tabEl = document.createElement('a');
    tabEl.className = 'tab';
    tabEl.textContent = serviceName;
    tabEl.href = '#' + encodeURIComponent(hashValue);

    let extLink = document.createElement('a');
    extLink.href = serviceUrl;
    extLink.title=`Открыть ${serviceName} во внешней вкладке`
    extLink.target = '_blank';
    extLink.textContent = '🔗';
    extLink.className = 'tab-ext-link';

    tabEl.appendChild(extLink);

    let contentEl = document.createElement('div');
    contentEl.className = 'content-container';
    let iframeEl = document.createElement('iframe');
    iframeEl.src = serviceUrl;
    contentEl.appendChild(iframeEl);

    tabContainer.appendChild(tabEl);
    contentWrapper.appendChild(contentEl);

    allTabs.push(tabEl);
    allContents.push(contentEl);
}


function tryActivateTabFromHash() {
    let rawHashValue = window.location.hash ? window.location.hash.substring(1) : '';
    let decodedHashValue = decodeURIComponent(rawHashValue);
    let idx = serviceHashes.indexOf(decodedHashValue);
    if (idx !== -1) {
        activateTab(idx);
    } else {
        activateTab(0);
    }
}

fetch(servicesUrl)
    .then(response => response.json())
    .then(data => {
        data.forEach(service => {
            serviceHashes.push(service.hash);
            let url = `https://${currentHost}:${service.port}`;
            createTab(service.name, url, service.hash);
        });

        tryActivateTabFromHash();
    })
    .catch(err => {
        console.error(`Error fetching ${servicesUrl}:`, err);
    });


window.addEventListener('hashchange', tryActivateTabFromHash);
