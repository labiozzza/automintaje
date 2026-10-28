(() => {
  window.openMotionPublishWindow = (jobId) => {
    if (!jobId || typeof window.openPublishWindow !== 'function') return;
    window.openPublishWindow({source:'motion', jobId:String(jobId)});
  };
})();
